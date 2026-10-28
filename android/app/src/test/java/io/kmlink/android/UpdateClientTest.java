package io.kmlink.android;

import java.io.ByteArrayInputStream;
import java.io.File;
import java.io.IOException;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.security.MessageDigest;
import org.junit.Rule;
import org.junit.Test;
import org.junit.rules.TemporaryFolder;
import static org.junit.Assert.*;

public final class UpdateClientTest {
    @Rule public TemporaryFolder temporary = new TemporaryFolder();
    private static final class Response extends HttpURLConnection {
        final byte[] body; final int status; final String redirect; final long length;
        boolean disconnected;
        Response(int status, byte[] body, long length, String redirect) throws Exception {
            super(new URL("https://github.com/")); this.status=status; this.body=body; this.length=length; this.redirect=redirect;
        }
        public void disconnect(){disconnected=true;}
        public boolean usingProxy(){return false;}
        public void connect(){}
        public int getResponseCode(){return status;}
        public String getHeaderField(String name){return "Location".equals(name)?redirect:null;}
        public long getContentLengthLong(){return length;}
        public InputStream getInputStream(){return new ByteArrayInputStream(body);}
    }
    private static UpdateManifest manifest(byte[] data, String digest) throws Exception {
        return UpdateManifest.parse(UpdateManifestTest.release("0.4.0", data.length, "sha256:"+digest));
    }
    @Test public void downloadChecksLengthAndDigestBeforePublishingStagingFile() throws Exception {
        byte[] data="test-apk-content".getBytes(StandardCharsets.UTF_8);
        String digest=UpdateClient.hex(MessageDigest.getInstance("SHA-256").digest(data));
        Response response=new Response(200,data,data.length,null);
        UpdateClient client=new UpdateClient(url->response);
        File directory=temporary.newFolder();long[] progress={0};
        File result=client.download(manifest(data,digest),directory,(done,total)->progress[0]=done);
        assertArrayEquals(data,Files.readAllBytes(result.toPath())); assertEquals(data.length,progress[0]);
        assertFalse(new File(directory,UpdateApkProvider.FILE_NAME).exists());
        assertTrue(response.disconnected);
    }
    @Test public void wrongDigestAndTruncationNeverLeaveAnInstallableFile() throws Exception {
        byte[] data="bad-package".getBytes(StandardCharsets.UTF_8);
        for (long length:new long[]{data.length,data.length-1}) {
            Response response=new Response(200,data,length,null);File directory=temporary.newFolder();
            try { new UpdateClient(url->response).download(manifest(data,String.join("",java.util.Collections.nCopies(64,"0"))),directory,(done,total)->{});fail(); }
            catch(IOException expected){}
            assertEquals(0,directory.list().length);assertTrue(response.disconnected);
        }
    }
    @Test public void cancelRemovesPartialDownload() throws Exception {
        byte[] data=new byte[70000];String digest=UpdateClient.hex(MessageDigest.getInstance("SHA-256").digest(data));
        Response response=new Response(200,data,data.length,null);UpdateClient client=new UpdateClient(url->response);
        File directory=temporary.newFolder();
        try{client.download(manifest(data,digest),directory,(done,total)->client.cancel());fail();}catch(IOException expected){}
        assertEquals(0,directory.list().length);assertTrue(response.disconnected);
    }
    @Test public void redirectsCannotEscapeAllowedHosts() throws Exception {
        Response first=new Response(302,new byte[0],0,"https://attacker.test/package");
        int[] opened={0};UpdateClient client=new UpdateClient(url->{opened[0]++;return first;});
        try{client.text(UpdateManifest.API,100);fail();}catch(IOException expected){}
        assertEquals(1,opened[0]);assertTrue(first.disconnected);
    }
    @Test public void textResponsesAreBoundedAndRateLimitsAreActionable() throws Exception {
        for(Response response:new Response[]{new Response(200,new byte[11],-1,null),new Response(403,new byte[0],0,null)}){
            try{new UpdateClient(url->response).text(UpdateManifest.API,10);fail();}catch(IOException expected){}
            assertTrue(response.disconnected);
        }
    }
}
