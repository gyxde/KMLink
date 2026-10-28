package io.kmlink.android;

import android.os.Build;
import android.security.keystore.KeyGenParameterSpec;
import android.security.keystore.KeyProperties;
import androidx.test.ext.junit.runners.AndroidJUnit4;
import java.math.BigInteger;
import java.net.InetAddress;
import java.security.KeyPairGenerator;
import java.security.KeyStore;
import java.security.MessageDigest;
import java.security.PrivateKey;
import java.security.SecureRandom;
import java.security.Signature;
import java.security.cert.X509Certificate;
import java.security.spec.ECGenParameterSpec;
import java.util.Arrays;
import java.util.Date;
import java.util.UUID;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;
import javax.net.ssl.SSLContext;
import javax.net.ssl.SSLServerSocket;
import javax.net.ssl.SSLSocket;
import javax.net.ssl.TrustManager;
import javax.net.ssl.X509TrustManager;
import javax.security.auth.x500.X500Principal;
import org.junit.Test;
import org.junit.runner.RunWith;
import static org.junit.Assert.*;

/** Exercises AndroidKeyStore and the device TLS provider, not JVM substitutes. */
@RunWith(AndroidJUnit4.class)
public final class TlsIdentityTest {
    @Test public void legacyIdentityIsReplacedAndCanSignTlsDigests() throws Exception {
        String alias=testAlias();KeyStore store=keyStore();
        try {
            // Reproduce the exact authorization defect from the first Android build.
            KeyPairGenerator generator=KeyPairGenerator.getInstance("EC","AndroidKeyStore");
            generator.initialize(new KeyGenParameterSpec.Builder(alias,KeyProperties.PURPOSE_SIGN|KeyProperties.PURPOSE_VERIFY)
                    .setAlgorithmParameterSpec(new ECGenParameterSpec("secp256r1"))
                    .setDigests(KeyProperties.DIGEST_SHA256,KeyProperties.DIGEST_SHA512)
                    .setCertificateSubject(new X500Principal("CN=KMLink legacy test"))
                    .setCertificateSerialNumber(BigInteger.ONE)
                    .setCertificateNotBefore(new Date(System.currentTimeMillis()-60_000))
                    .setCertificateNotAfter(new Date(System.currentTimeMillis()+86_400_000)).build());
            generator.generateKeyPair();
            byte[] original=store.getCertificate(alias).getEncoded();
            boolean rejected=false;
            try { signDigest((PrivateKey)store.getKey(alias,null)); }
            catch(Exception expected) { rejected=true; }
            assertTrue("Legacy digest authorizations must reject TLS raw signing",rejected);

            AndroidTlsIdentity upgraded=new AndroidTlsIdentity(alias);
            assertFalse("Legacy key must be regenerated with immutable authorizations",Arrays.equals(original,upgraded.certificateDer()));
            byte[] signature=signDigest((PrivateKey)store.getKey(alias,null));
            Signature verify=Signature.getInstance("NONEwithECDSA");
            verify.initVerify(store.getCertificate(alias).getPublicKey());verify.update(digest());
            assertTrue("AndroidKeyStore must sign the TLS transcript digest",verify.verify(signature));
            assertArrayEquals("A compatible identity must remain stable across starts",upgraded.certificateDer(),new AndroidTlsIdentity(alias).certificateDer());
        } finally { store.deleteEntry(alias); }
    }

    @Test public void realKeystoreIdentityCompletesTls12AndTls13() throws Exception {
        String alias=testAlias();KeyStore store=keyStore();
        try {
            AndroidTlsIdentity identity=new AndroidTlsIdentity(alias);
            handshake(identity,"TLSv1.2");
            if(Build.VERSION.SDK_INT>=29)handshake(identity,"TLSv1.3");
        } finally { store.deleteEntry(alias); }
    }

    private static void handshake(AndroidTlsIdentity identity,String protocol) throws Exception {
        ExecutorService executor=Executors.newSingleThreadExecutor();
        try(SSLServerSocket server=(SSLServerSocket)identity.serverContext().getServerSocketFactory()
                .createServerSocket(0,1,InetAddress.getByName("127.0.0.1"))){
            server.setSoTimeout(6000);server.setEnabledProtocols(new String[]{protocol});
            Future<String> serverResult=executor.submit(()->{
                try(SSLSocket accepted=(SSLSocket)server.accept()){
                    accepted.setSoTimeout(6000);accepted.startHandshake();
                    accepted.getOutputStream().write(75);accepted.getOutputStream().flush();
                    return accepted.getSession().getProtocol();
                }
            });
            SSLContext client=SSLContext.getInstance("TLS");
            byte[] expected=identity.certificateDer();
            client.init(null,new TrustManager[]{new X509TrustManager(){
                public X509Certificate[] getAcceptedIssuers(){return new X509Certificate[0];}
                public void checkClientTrusted(X509Certificate[] chain,String authType){}
                public void checkServerTrusted(X509Certificate[] chain,String authType) throws java.security.cert.CertificateException {
                    if(chain.length==0||!MessageDigest.isEqual(expected,chain[0].getEncoded()))throw new java.security.cert.CertificateException("Unexpected local test identity");
                }
            }},new SecureRandom());
            try(SSLSocket socket=(SSLSocket)client.getSocketFactory().createSocket("127.0.0.1",server.getLocalPort())){
                socket.setEnabledProtocols(new String[]{protocol});socket.setSoTimeout(6000);socket.startHandshake();
                assertEquals(protocol,socket.getSession().getProtocol());assertEquals(75,socket.getInputStream().read());
            } catch(Exception clientFailure){
                try { serverResult.get(7,TimeUnit.SECONDS); }
                catch(Exception serverFailure){clientFailure.addSuppressed(serverFailure);}
                throw new AssertionError(protocol+" AndroidKeyStore handshake failed (server cause included)",clientFailure);
            }
            assertEquals(protocol,serverResult.get(7,TimeUnit.SECONDS));
        } finally { executor.shutdownNow(); }
    }
    private static String testAlias(){return "kmlink-test-"+UUID.randomUUID();}
    private static KeyStore keyStore() throws Exception {KeyStore store=KeyStore.getInstance("AndroidKeyStore");store.load(null);return store;}
    private static byte[] digest() throws Exception {return MessageDigest.getInstance("SHA-256").digest("KMLink TLS signing test".getBytes(java.nio.charset.StandardCharsets.UTF_8));}
    private static byte[] signDigest(PrivateKey key) throws Exception {
        Signature signer=Signature.getInstance("NONEwithECDSA");signer.initSign(key);signer.update(digest());return signer.sign();
    }
}
