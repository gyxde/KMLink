package io.kmlink.protocol;

import javax.net.ssl.SSLContext;

/** The app provides an Android Keystore-backed TLS server identity. */
public interface TlsIdentity {
  SSLContext serverContext();

  byte[] certificateDer();
}
