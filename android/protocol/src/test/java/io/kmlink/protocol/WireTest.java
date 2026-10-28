package io.kmlink.protocol;

import static org.junit.Assert.*;

import java.io.*;
import java.util.*;
import org.json.*;
import org.junit.Test;

public class WireTest {
  private byte[] range(int start) {
    byte[] b = new byte[32];
    for (int i = 0; i < 32; i++) b[i] = (byte) (start + i);
    return b;
  }

  @Test
  public void matchesPythonProofAndComparisonVector() throws Exception {
    byte[] c = range(0), s = range(32), d = range(64);
    assertEquals("AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8", Wire.base64(c));
    assertArrayEquals(c, Wire.unbase64(Wire.base64(c)));
    assertArrayEquals(c, Wire.key(Wire.base64(c) + "\n"));
    assertEquals(
        "40e095356243414299d0bf4499904a86cd1d23a65207f1e748c6a53c6db0e889", Wire.commitment(c));
    assertEquals(
        "64b059d4255b4649bfe63c63a2eaa5d9cffa23bac4bafaf674398edb4b7980a8", Wire.commitment(s));
    assertEquals(
        "fcb8322746f8272735eba6171ac9d0b6f4401cb2a5ae7639bb20f5006d0ec2cb",
        Wire.proof(c, "server", Wire.transcript(d, c, s)));
    JSONObject request =
        Wire.obj(
            "type",
            "request",
            "protocol",
            KMLinkNode.PROTOCOL,
            "request_id",
            "11111111111111111111111111111111",
            "target_id",
            "pc-uuid",
            "device",
            Wire.obj("id", "android-uuid", "name", "设备 🚀", "system", "Android"),
            "mode",
            "control",
            "nonce_commit",
            Wire.commitment(c));
    JSONObject challenge =
        Wire.obj(
            "type",
            "challenge",
            "protocol",
            KMLinkNode.PROTOCOL,
            "request_id",
            "11111111111111111111111111111111",
            "device",
            Wire.obj("id", "pc-uuid", "name", "台式机", "system", "Windows"),
            "nonce_commit",
            Wire.commitment(s));
    assertEquals("678394", Wire.comparisonCode(d, request, challenge, c, s));
    assertNotEquals("678394", Wire.comparisonCode(range(65), request, challenge, c, s));
  }

  @Test
  public void canonicalUnicodeAndJsonRoundTrip() throws Exception {
    JSONObject o = Wire.obj("z", "中文🚀\n\\\"", "a", Wire.obj("b", true, "a", 1));
    assertEquals("{\"a\":{\"a\":1,\"b\":true},\"z\":\"中文🚀\\n\\\\\\\"\"}", Wire.canonical(o));
    assertEquals(Wire.canonical(o), Wire.canonical(Wire.parse(Wire.utf8(Wire.canonical(o)))));
    ByteArrayOutputStream out = new ByteArrayOutputStream();
    Wire.write(out, o, 1024);
    Wire.Reader reader =
        new Wire.Reader(new ByteArrayInputStream(out.toByteArray()), () -> false, 1024);
    assertEquals(Wire.canonical(o), Wire.canonical(reader.poll()));
  }

  @Test
  public void rejectsLenientDuplicateDeepOrInvalidUtf8Json() throws Exception {
    for (String bad :
        Arrays.asList(
            "{\"a\":1,\"a\":2}",
            "{x:1}",
            "{\"x\":NaN}",
            "{\"x\":01}",
            "{} trailing",
            "[1]",
            "{\"x\":\"\\ud800\"}")) {
      try {
        Wire.parse(Wire.utf8(bad));
        fail(bad);
      } catch (IOException expected) {
      }
    }
    try {
      Wire.parse(new byte[] {'{', '"', 'x', '"', ':', '"', (byte) 0xff, '"', '}'});
      fail();
    } catch (IOException expected) {
    }
    String deep = "0";
    for (int i = 0; i < 40; i++) deep = "[" + deep + "]";
    try {
      Wire.parse(Wire.utf8("{\"x\":" + deep + "}"));
      fail();
    } catch (IOException expected) {
    }
  }

  @Test
  public void identityTextMatchesPythonPrintableRules() throws Exception {
    assertEquals("设备 🚀", Wire.requireText("设备 🚀", 80));
    for (String text : Arrays.asList("a\u2028b", "a\u00a0b", "a\ue000b", "a\u0000b")) {
      try {
        Wire.requireText(text, 80);
        fail();
      } catch (IOException expected) {
      }
    }
  }

  @Test
  public void framesAndKeysAreBounded() throws Exception {
    try {
      new Wire.Reader(new ByteArrayInputStream(new byte[] {127, -1, -1, -1}), () -> false, 8192)
          .poll();
      fail();
    } catch (IOException expected) {
    }
    try {
      Wire.key("YWJj");
      fail();
    } catch (IOException expected) {
    }
    try {
      Wire.unbase64("AB");
      fail();
    } catch (IOException expected) {
    }
    assertFalse(Wire.equalHex("zz", Wire.hex(new byte[32])));
    try {
      Wire.requirePort(true);
      fail();
    } catch (IOException expected) {
    }
  }
}
