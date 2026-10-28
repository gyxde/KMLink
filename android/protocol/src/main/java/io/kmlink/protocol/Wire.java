package io.kmlink.protocol;

import java.io.*;
import java.net.*;
import java.nio.*;
import java.nio.charset.*;
import java.security.*;
import java.util.*;
import java.util.function.BooleanSupplier;
import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;
import javax.net.ssl.*;
import org.json.*;

/** Wire primitives shared with src/kmlink/{transport,pairing}.py. */
public final class Wire {
  public static final int MAX_FRAME = 256 * 1024, MAX_INVITE = 8192;
  private static final SecureRandom RANDOM = new SecureRandom();
  private static final char[] BASE64 =
      "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_".toCharArray();

  private Wire() {}

  public static JSONObject obj(Object... pairs) {
    if ((pairs.length & 1) != 0) throw new IllegalArgumentException("Odd object arguments");
    JSONObject result = new JSONObject();
    try {
      for (int i = 0; i < pairs.length; i += 2) result.put((String) pairs[i], pairs[i + 1]);
    } catch (Exception e) {
      throw new IllegalArgumentException("Invalid JSON object", e);
    }
    return result;
  }

  public static byte[] random(int n) {
    byte[] bytes = new byte[n];
    RANDOM.nextBytes(bytes);
    return bytes;
  }

  public static byte[] utf8(String text) {
    return text.getBytes(StandardCharsets.UTF_8);
  }

  public static byte[] sha256(byte[]... parts) {
    try {
      MessageDigest md = MessageDigest.getInstance("SHA-256");
      for (byte[] part : parts) md.update(part);
      return md.digest();
    } catch (GeneralSecurityException e) {
      throw new IllegalStateException(e);
    }
  }

  public static String hex(byte[] value) {
    StringBuilder text = new StringBuilder(value.length * 2);
    for (byte b : value)
      text.append(Character.forDigit((b & 255) >>> 4, 16)).append(Character.forDigit(b & 15, 16));
    return text.toString();
  }

  public static String base64(byte[] bytes) {
    StringBuilder s = new StringBuilder((bytes.length * 4 + 2) / 3);
    for (int i = 0; i < bytes.length; i += 3) {
      int n = (bytes[i] & 255) << 16;
      if (i + 1 < bytes.length) n |= (bytes[i + 1] & 255) << 8;
      if (i + 2 < bytes.length) n |= bytes[i + 2] & 255;
      s.append(BASE64[(n >>> 18) & 63]).append(BASE64[(n >>> 12) & 63]);
      if (i + 1 < bytes.length) s.append(BASE64[(n >>> 6) & 63]);
      if (i + 2 < bytes.length) s.append(BASE64[n & 63]);
    }
    return s.toString();
  }

  public static byte[] unbase64(String value) throws IOException {
    if (value == null || !value.matches("[A-Za-z0-9_-]+={0,2}"))
      throw new IOException("Invalid base64url");
    String s = value.replaceAll("=+$", "");
    if (s.length() % 4 == 1) throw new IOException("Invalid base64url length");
    ByteArrayOutputStream out = new ByteArrayOutputStream();
    int accumulator = 0, bits = 0;
    for (int i = 0; i < s.length(); i++) {
      int v =
          "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_".indexOf(s.charAt(i));
      accumulator = (accumulator << 6) | v;
      bits += 6;
      if (bits >= 8) {
        bits -= 8;
        out.write((accumulator >>> bits) & 255);
      }
    }
    byte[] decoded = out.toByteArray();
    if (!base64(decoded).equals(s)) throw new IOException("Noncanonical base64url");
    return decoded;
  }

  public static byte[] key(String text) throws IOException {
    if (text == null) throw new IOException("Missing key");
    byte[] key = unbase64(text.replaceAll("\\s+", ""));
    if (key.length < 32 || key.length > 64) throw new IOException("Invalid key length");
    return key;
  }

  public static String proof(byte[] key, String role, byte[] transcript) {
    try {
      Mac mac = Mac.getInstance("HmacSHA256");
      mac.init(new SecretKeySpec(key, "HmacSHA256"));
      mac.update(utf8(role + "\0"));
      return hex(mac.doFinal(transcript));
    } catch (GeneralSecurityException e) {
      throw new IllegalStateException(e);
    }
  }

  public static boolean equalHex(Object supplied, String expected) {
    if (!(supplied instanceof String) || !((String) supplied).matches("[0-9a-f]{64}")) return false;
    return MessageDigest.isEqual(utf8((String) supplied), utf8(expected));
  }

  public static byte[] concat(byte[]... parts) {
    int n = 0;
    for (byte[] p : parts) n += p.length;
    byte[] all = new byte[n];
    int offset = 0;
    for (byte[] p : parts) {
      System.arraycopy(p, 0, all, offset, p.length);
      offset += p.length;
    }
    return all;
  }

  public static byte[] transcript(byte[] digest, byte[] client, byte[] server) {
    return concat(utf8("KM Link pairing v1\0"), digest, client, server);
  }

  public static String commitment(byte[] nonce) {
    return hex(sha256(utf8("KMLink invite nonce\0"), nonce));
  }

  public static String comparisonCode(
      byte[] digest, JSONObject request, JSONObject challenge, byte[] client, byte[] server) {
    JSONArray transcript = new JSONArray();
    transcript.put(request);
    transcript.put(challenge);
    byte[] h =
        sha256(
            utf8("KMLink numeric comparison v1\0"),
            digest,
            client,
            server,
            utf8(canonical(transcript)));
    long remainder = 0;
    for (int i = 0; i < 8; i++) remainder = (remainder * 256 + (h[i] & 255)) % 1000000;
    return String.format(Locale.ROOT, "%06d", remainder);
  }

  public static String canonical(Object value) {
    StringBuilder out = new StringBuilder();
    writeJson(value, out, 0);
    return out.toString();
  }

  private static void writeJson(Object value, StringBuilder out, int depth) {
    if (depth > 32) throw new IllegalArgumentException("JSON too deep");
    if (value == null || value == JSONObject.NULL) {
      out.append("null");
      return;
    }
    if (value instanceof JSONObject) {
      JSONObject object = (JSONObject) value;
      ArrayList<String> keys = new ArrayList<>();
      Iterator<String> it = object.keys();
      while (it.hasNext()) keys.add(it.next());
      Collections.sort(keys);
      out.append('{');
      boolean comma = false;
      for (String k : keys) {
        if (comma) out.append(',');
        comma = true;
        quote(k, out);
        out.append(':');
        writeJson(object.opt(k), out, depth + 1);
      }
      out.append('}');
      return;
    }
    if (value instanceof JSONArray) {
      JSONArray a = (JSONArray) value;
      out.append('[');
      for (int i = 0; i < a.length(); i++) {
        if (i > 0) out.append(',');
        writeJson(a.opt(i), out, depth + 1);
      }
      out.append(']');
      return;
    }
    if (value instanceof String) {
      quote((String) value, out);
      return;
    }
    if (value instanceof Boolean) {
      out.append(value);
      return;
    }
    if (value instanceof Number) {
      double d = ((Number) value).doubleValue();
      if (Double.isNaN(d) || Double.isInfinite(d))
        throw new IllegalArgumentException("Nonfinite JSON");
      try {
        out.append(JSONObject.numberToString((Number) value));
      } catch (JSONException e) {
        // Android's JSONException is checked; the JVM implementation is not.
        throw new IllegalArgumentException("Invalid JSON number", e);
      }
      return;
    }
    throw new IllegalArgumentException("Unsupported JSON value");
  }

  private static void quote(String value, StringBuilder out) {
    out.append('"');
    for (int i = 0; i < value.length(); i++) {
      char c = value.charAt(i);
      switch (c) {
        case '"':
          out.append("\\\"");
          break;
        case '\\':
          out.append("\\\\");
          break;
        case '\b':
          out.append("\\b");
          break;
        case '\f':
          out.append("\\f");
          break;
        case '\n':
          out.append("\\n");
          break;
        case '\r':
          out.append("\\r");
          break;
        case '\t':
          out.append("\\t");
          break;
        default:
          if (c < 32) out.append(String.format(Locale.ROOT, "\\u%04x", (int) c));
          else {
            if (Character.isHighSurrogate(c)) {
              if (i + 1 >= value.length() || !Character.isLowSurrogate(value.charAt(i + 1)))
                throw new IllegalArgumentException("Invalid Unicode");
              out.append(c).append(value.charAt(++i));
            } else if (Character.isLowSurrogate(c))
              throw new IllegalArgumentException("Invalid Unicode");
            else out.append(c);
          }
      }
    }
    out.append('"');
  }

  public static JSONObject parse(byte[] bytes) throws IOException {
    try {
      String text =
          StandardCharsets.UTF_8
              .newDecoder()
              .onMalformedInput(CodingErrorAction.REPORT)
              .onUnmappableCharacter(CodingErrorAction.REPORT)
              .decode(ByteBuffer.wrap(bytes))
              .toString();
      Object result = new Parser(text).parse();
      if (!(result instanceof JSONObject)) throw new IOException("Expected JSON object");
      return (JSONObject) result;
    } catch (CharacterCodingException | IllegalArgumentException e) {
      throw new IOException("Invalid JSON", e);
    }
  }

  public static boolean integer(Object value) {
    return value instanceof Integer || value instanceof Long;
  }

  public static int requirePort(Object value) throws IOException {
    if (!integer(value) || ((Number) value).longValue() < 1 || ((Number) value).longValue() > 65535)
      throw new IOException("Invalid port");
    return ((Number) value).intValue();
  }

  public static String requireText(Object value, int max) throws IOException {
    if (!(value instanceof String)) throw new IOException("Invalid text");
    String s = (String) value;
    if (s.length() < 1
        || s.length() > max
        || !s.equals(s.trim())
        || Character.isWhitespace(s.codePointAt(0))
        || Character.isSpaceChar(s.codePointAt(0))
        || Character.isWhitespace(s.codePointBefore(s.length()))
        || Character.isSpaceChar(s.codePointBefore(s.length())))
      throw new IOException("Invalid text");
    for (int i = 0; i < s.length(); ) {
      int c = s.codePointAt(i);
      int category = Character.getType(c);
      if (category == Character.CONTROL
          || category == Character.FORMAT
          || category == Character.SURROGATE
          || category == Character.PRIVATE_USE
          || category == Character.UNASSIGNED
          || category == Character.LINE_SEPARATOR
          || category == Character.PARAGRAPH_SEPARATOR
          || category == Character.SPACE_SEPARATOR && c != 32)
        throw new IOException("Invalid text");
      i += Character.charCount(c);
    }
    return s;
  }

  public static String requireId(Object value) throws IOException {
    String id = requireText(value, 80);
    if (!id.matches("[A-Za-z0-9_.:-]{1,80}")) throw new IOException("Invalid device id");
    return id;
  }

  public static byte[] nonce(Object value) throws IOException {
    if (!(value instanceof String)) throw new IOException("Missing nonce");
    byte[] n = unbase64((String) value);
    if (n.length != 32 || ((String) value).length() != 43) throw new IOException("Invalid nonce");
    return n;
  }

  public static void write(OutputStream out, JSONObject object, int max) throws IOException {
    byte[] data = utf8(canonical(object));
    if (data.length < 1 || data.length > max) throw new IOException("Frame too large");
    synchronized (out) {
      out.write(
          new byte[] {
            (byte) (data.length >>> 24),
            (byte) (data.length >>> 16),
            (byte) (data.length >>> 8),
            (byte) data.length
          });
      out.write(data);
      out.flush();
    }
  }

  /** A short socket timeout returns null only before the first frame byte. */
  public static final class Reader {
    private final InputStream in;
    private final BooleanSupplier stopped;
    private final int max;

    public Reader(InputStream in, BooleanSupplier stopped, int max) {
      this.in = in;
      this.stopped = stopped;
      this.max = max;
    }

    public JSONObject poll() throws IOException {
      if (stopped.getAsBoolean()) throw new InterruptedIOException("Stopped");
      int first;
      try {
        first = in.read();
      } catch (SocketTimeoutException e) {
        return null;
      }
      if (first < 0) throw new EOFException("Disconnected");
      byte[] header = new byte[4];
      header[0] = (byte) first;
      long deadline = System.nanoTime() + 3_000_000_000L;
      readFully(header, 1, 3, deadline);
      int length = ByteBuffer.wrap(header).getInt();
      if (length < 1 || length > max) throw new IOException("Invalid frame length");
      byte[] data = new byte[length];
      readFully(data, 0, length, deadline);
      return parse(data);
    }

    public JSONObject await(long timeoutMillis) throws IOException {
      long until = System.nanoTime() + timeoutMillis * 1_000_000L;
      while (System.nanoTime() < until) {
        JSONObject o = poll();
        if (o != null) return o;
      }
      throw new SocketTimeoutException("Response timeout");
    }

    private void readFully(byte[] dest, int offset, int length, long deadline) throws IOException {
      while (length > 0) {
        if (stopped.getAsBoolean()) throw new InterruptedIOException("Stopped");
        if (System.nanoTime() > deadline) throw new SocketTimeoutException("Incomplete frame");
        int n;
        try {
          n = in.read(dest, offset, length);
        } catch (SocketTimeoutException e) {
          continue;
        }
        if (n < 0) throw new EOFException("Disconnected");
        offset += n;
        length -= n;
      }
    }
  }

  public static SSLContext clientContext() {
    try {
      SSLContext context = SSLContext.getInstance("TLS");
      context.init(
          null,
          new TrustManager[] {
            new X509TrustManager() {
              public java.security.cert.X509Certificate[] getAcceptedIssuers() {
                return new java.security.cert.X509Certificate[0];
              }

              public void checkClientTrusted(java.security.cert.X509Certificate[] c, String a) {}

              public void checkServerTrusted(java.security.cert.X509Certificate[] c, String a) {}
            }
          },
          RANDOM);
      return context;
    } catch (GeneralSecurityException e) {
      throw new IllegalStateException(e);
    }
  }

  public static void configure(SSLSocket socket) throws IOException {
    List<String> protocols = new ArrayList<>();
    for (String p : socket.getSupportedProtocols())
      if (p.equals("TLSv1.2") || p.equals("TLSv1.3")) protocols.add(p);
    if (protocols.isEmpty()) throw new IOException("TLS 1.2 unavailable");
    socket.setEnabledProtocols(protocols.toArray(new String[0]));
    socket.setSoTimeout(3000);
    socket.startHandshake();
    socket.setSoTimeout(150);
  }

  static void close(Closeable socket) {
    if (socket != null)
      try {
        socket.close();
      } catch (IOException ignored) {
      }
  }

  /** Small strict parser: rejects duplicate keys, lenient JSON and excessive nesting. */
  private static final class Parser {
    final String s;
    int p;

    Parser(String s) {
      this.s = s;
    }

    Object parse() throws IOException {
      Object v = value(0);
      ws();
      if (p != s.length()) bad();
      return v;
    }

    void ws() {
      while (p < s.length()
          && (s.charAt(p) == ' '
              || s.charAt(p) == '\n'
              || s.charAt(p) == '\r'
              || s.charAt(p) == '\t')) p++;
    }

    void bad() throws IOException {
      throw new IOException("Invalid JSON at offset " + p);
    }

    Object value(int depth) throws IOException {
      ws();
      if (depth > 32 || p >= s.length()) bad();
      char c = s.charAt(p);
      if (c == '{') return object(depth + 1);
      if (c == '[') return array(depth + 1);
      if (c == '"') return string();
      if (s.startsWith("true", p)) {
        p += 4;
        return true;
      }
      if (s.startsWith("false", p)) {
        p += 5;
        return false;
      }
      if (s.startsWith("null", p)) {
        p += 4;
        return JSONObject.NULL;
      }
      return number();
    }

    JSONObject object(int d) throws IOException {
      p++;
      JSONObject o = new JSONObject();
      HashSet<String> keys = new HashSet<>();
      ws();
      if (take('}')) return o;
      while (true) {
        ws();
        if (p >= s.length() || s.charAt(p) != '"') bad();
        String k = string();
        if (!keys.add(k)) bad();
        ws();
        if (!take(':')) bad();
        Object v = value(d);
        try {
          o.put(k, v);
        } catch (Exception e) {
          throw new IOException(e);
        }
        ws();
        if (take('}')) return o;
        if (!take(',')) bad();
      }
    }

    JSONArray array(int d) throws IOException {
      p++;
      JSONArray a = new JSONArray();
      ws();
      if (take(']')) return a;
      while (true) {
        a.put(value(d));
        ws();
        if (take(']')) return a;
        if (!take(',')) bad();
      }
    }

    boolean take(char c) {
      if (p < s.length() && s.charAt(p) == c) {
        p++;
        return true;
      }
      return false;
    }

    String string() throws IOException {
      p++;
      StringBuilder b = new StringBuilder();
      while (p < s.length()) {
        char c = s.charAt(p++);
        if (c == '"') {
          String result = b.toString();
          for (int i = 0; i < result.length(); i++) {
            char x = result.charAt(i);
            if (Character.isHighSurrogate(x)) {
              if (++i >= result.length() || !Character.isLowSurrogate(result.charAt(i))) bad();
            } else if (Character.isLowSurrogate(x)) bad();
          }
          return result;
        }
        if (c < 32) bad();
        if (c != '\\') {
          b.append(c);
          continue;
        }
        if (p >= s.length()) bad();
        c = s.charAt(p++);
        switch (c) {
          case '"':
          case '\\':
          case '/':
            b.append(c);
            break;
          case 'b':
            b.append('\b');
            break;
          case 'f':
            b.append('\f');
            break;
          case 'n':
            b.append('\n');
            break;
          case 'r':
            b.append('\r');
            break;
          case 't':
            b.append('\t');
            break;
          case 'u':
            if (p + 4 > s.length()) bad();
            try {
              b.append((char) Integer.parseInt(s.substring(p, p + 4), 16));
            } catch (NumberFormatException e) {
              bad();
            }
            p += 4;
            break;
          default:
            bad();
        }
      }
      bad();
      return null;
    }

    Number number() throws IOException {
      int start = p;
      if (take('-')) {}
      if (p >= s.length()) bad();
      if (take('0')) {
        if (p < s.length() && Character.isDigit(s.charAt(p))) bad();
      } else {
        if (p >= s.length() || s.charAt(p) < '1' || s.charAt(p) > '9') bad();
        while (p < s.length() && s.charAt(p) >= '0' && s.charAt(p) <= '9') p++;
      }
      boolean floating = false;
      if (take('.')) {
        floating = true;
        int pos = p;
        while (p < s.length() && s.charAt(p) >= '0' && s.charAt(p) <= '9') p++;
        if (pos == p) bad();
      }
      if (p < s.length() && (s.charAt(p) == 'e' || s.charAt(p) == 'E')) {
        p++;
        floating = true;
        if (p < s.length() && (s.charAt(p) == '+' || s.charAt(p) == '-')) p++;
        int pos = p;
        while (p < s.length() && s.charAt(p) >= '0' && s.charAt(p) <= '9') p++;
        if (pos == p) bad();
      }
      try {
        String n = s.substring(start, p);
        if (!floating) return Long.parseLong(n);
        double v = Double.parseDouble(n);
        if (Double.isNaN(v) || Double.isInfinite(v)) bad();
        return v;
      } catch (NumberFormatException e) {
        throw new IOException("Invalid number", e);
      }
    }
  }
}
