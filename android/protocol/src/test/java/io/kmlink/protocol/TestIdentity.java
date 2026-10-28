package io.kmlink.protocol;

import java.io.*;
import java.nio.file.*;
import java.security.*;
import java.security.cert.Certificate;
import javax.net.ssl.*;

final class TestIdentity implements TlsIdentity {
  private final SSLContext context;
  private final byte[] der;

  TestIdentity(Path path, String password) throws Exception {
    KeyStore store = KeyStore.getInstance("PKCS12");
    try (InputStream in = Files.newInputStream(path)) {
      store.load(in, password.toCharArray());
    }
    String alias = store.aliases().nextElement();
    Certificate cert = store.getCertificate(alias);
    der = cert.getEncoded();
    KeyManagerFactory managers =
        KeyManagerFactory.getInstance(KeyManagerFactory.getDefaultAlgorithm());
    managers.init(store, password.toCharArray());
    context = SSLContext.getInstance("TLS");
    context.init(managers.getKeyManagers(), null, new SecureRandom());
  }

  static TestIdentity temporary() throws Exception {
    Path directory = Files.createTempDirectory("kmlink-protocol-test-");
    Path path = directory.resolve("test.p12");
    String executable =
        System.getProperty("java.home")
            + File.separator
            + "bin"
            + File.separator
            + "keytool"
            + (System.getProperty("os.name").startsWith("Windows") ? ".exe" : "");
    Process process =
        new ProcessBuilder(
                executable,
                "-genkeypair",
                "-alias",
                "test",
                "-keystore",
                path.toString(),
                "-storetype",
                "PKCS12",
                "-keyalg",
                "EC",
                "-groupname",
                "secp256r1",
                "-dname",
                "CN=KMLink-Test",
                "-storepass",
                "testpassword",
                "-keypass",
                "testpassword",
                "-validity",
                "1",
                "-noprompt")
            .redirectErrorStream(true)
            .start();
    ByteArrayOutputStream log = new ByteArrayOutputStream();
    process.getInputStream().transferTo(log);
    if (process.waitFor() != 0) throw new IOException(log.toString("UTF-8"));
    TestIdentity result = new TestIdentity(path, "testpassword");
    Files.delete(path);
    Files.delete(directory);
    return result;
  }

  public SSLContext serverContext() {
    return context;
  }

  public byte[] certificateDer() {
    return der.clone();
  }
}
