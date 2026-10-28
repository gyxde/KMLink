package io.kmlink.android;

import android.security.keystore.KeyGenParameterSpec;
import android.security.keystore.KeyProperties;
import java.math.BigInteger;
import java.security.KeyPairGenerator;
import java.security.KeyStore;
import java.security.PrivateKey;
import java.security.Principal;
import java.security.SecureRandom;
import java.security.cert.X509Certificate;
import java.security.spec.ECGenParameterSpec;
import java.util.Calendar;
import java.util.Date;
import javax.net.ssl.SSLContext;
import javax.net.ssl.X509ExtendedKeyManager;
import javax.net.ssl.SSLEngine;
import javax.security.auth.x500.X500Principal;
import java.net.Socket;
import io.kmlink.protocol.TlsIdentity;

/** The private TLS identity remains non-exportable in AndroidKeyStore. */
final class AndroidTlsIdentity implements TlsIdentity {
    private static final String ALIAS = "kmlink-local-tls-v1";
    private final SSLContext context;
    private final byte[] certificate;

    AndroidTlsIdentity() throws Exception {
        KeyStore store = KeyStore.getInstance("AndroidKeyStore");
        store.load(null);
        if (!store.containsAlias(ALIAS)) {
            Calendar end = Calendar.getInstance(); end.add(Calendar.YEAR, 10);
            KeyPairGenerator generator = KeyPairGenerator.getInstance(KeyProperties.KEY_ALGORITHM_EC, "AndroidKeyStore");
            generator.initialize(new KeyGenParameterSpec.Builder(ALIAS, KeyProperties.PURPOSE_SIGN | KeyProperties.PURPOSE_VERIFY)
                    .setAlgorithmParameterSpec(new ECGenParameterSpec("secp256r1"))
                    .setDigests(KeyProperties.DIGEST_SHA256, KeyProperties.DIGEST_SHA512)
                    .setCertificateSubject(new X500Principal("CN=KMLink Android local peer"))
                    .setCertificateSerialNumber(new BigInteger(128, new SecureRandom()).abs().add(BigInteger.ONE))
                    .setCertificateNotBefore(new Date(System.currentTimeMillis() - 86_400_000L))
                    .setCertificateNotAfter(end.getTime()).build());
            generator.generateKeyPair();
        }
        PrivateKey key = (PrivateKey) store.getKey(ALIAS, null);
        X509Certificate cert = (X509Certificate) store.getCertificate(ALIAS);
        certificate = cert.getEncoded();
        X509ExtendedKeyManager manager = new X509ExtendedKeyManager() {
            public String[] getClientAliases(String type, Principal[] issuers) { return null; }
            public String chooseClientAlias(String[] types, Principal[] issuers, Socket socket) { return null; }
            public String[] getServerAliases(String type, Principal[] issuers) {
                return type != null && type.toUpperCase(java.util.Locale.ROOT).startsWith("EC") ? new String[]{ALIAS} : null;
            }
            public String chooseServerAlias(String type, Principal[] issuers, Socket socket) {
                return getServerAliases(type, issuers) == null ? null : ALIAS;
            }
            public String chooseEngineServerAlias(String type, Principal[] issuers, SSLEngine engine) {
                return chooseServerAlias(type, issuers, null);
            }
            public X509Certificate[] getCertificateChain(String alias) { return ALIAS.equals(alias) ? new X509Certificate[]{cert} : null; }
            public PrivateKey getPrivateKey(String alias) { return ALIAS.equals(alias) ? key : null; }
        };
        context = SSLContext.getInstance("TLS");
        context.init(new javax.net.ssl.KeyManager[]{manager}, null, new SecureRandom());
    }
    @Override public SSLContext serverContext() { return context; }
    @Override public byte[] certificateDer() { return certificate.clone(); }
}
