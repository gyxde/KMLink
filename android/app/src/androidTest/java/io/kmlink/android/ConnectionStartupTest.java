package io.kmlink.android;

import android.Manifest;
import android.app.Instrumentation;
import android.content.Context;
import android.graphics.Bitmap;
import android.os.Build;
import android.os.SystemClock;
import android.view.View;
import android.view.ViewGroup;
import android.widget.Button;
import androidx.test.core.app.ActivityScenario;
import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.platform.app.InstrumentationRegistry;
import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.security.SecureRandom;
import java.security.cert.X509Certificate;
import java.util.concurrent.atomic.AtomicBoolean;
import javax.net.ssl.SSLContext;
import javax.net.ssl.SSLSocket;
import javax.net.ssl.TrustManager;
import javax.net.ssl.X509TrustManager;
import org.junit.Test;
import org.junit.runner.RunWith;
import static org.junit.Assert.*;

/** Runs on an emulator/device. No accessibility permission or global input is granted. */
@RunWith(AndroidJUnit4.class)
public final class ConnectionStartupTest {
    @Test public void startDiscoveryUsesAndroidKeystoreAndStopsCleanly() throws Exception {
        Instrumentation instrumentation=InstrumentationRegistry.getInstrumentation();
        Context context=instrumentation.getTargetContext();
        if(Build.VERSION.SDK_INT>=33)instrumentation.getUiAutomation().grantRuntimePermission(context.getPackageName(),Manifest.permission.POST_NOTIFICATIONS);
        try(ActivityScenario<MainActivity> scenario=ActivityScenario.launch(MainActivity.class)){
            waitFor(()->LinkService.current()!=null,instrumentation,5000);
            saveScreenshot(instrumentation,context,"home.png");
            scenario.onActivity(activity->{Button start=findButton(activity.getWindow().getDecorView(),"开始发现设备");assertNotNull(start);assertTrue(start.performClick());});
            waitFor(()->LinkService.current()!=null&&LinkService.current().isDiscoveryReady(),instrumentation,12000);
            final int[] port={0};
            instrumentation.runOnMainSync(()->{LinkService service=LinkService.current();assertTrue(service.isRunning());assertFalse(service.isConnected());assertTrue(service.status().contains("发现"));port[0]=service.invitationPort();});
            saveScreenshot(instrumentation,context,"connected-service.png");
            // A real TLS handshake proves that the non-exportable AndroidKeyStore
            // private key works through the app's X509KeyManager, not only on JVM.
            SSLContext client=SSLContext.getInstance("TLS");
            client.init(null,new TrustManager[]{new X509TrustManager(){
                public X509Certificate[] getAcceptedIssuers(){return new X509Certificate[0];}
                public void checkClientTrusted(X509Certificate[] chain,String authType){}
                public void checkServerTrusted(X509Certificate[] chain,String authType){}
            }},new SecureRandom());
            try(SSLSocket socket=(SSLSocket)client.getSocketFactory().createSocket("127.0.0.1",port[0])){
                socket.setSoTimeout(4000);socket.startHandshake();assertTrue(socket.getSession().getPeerCertificates().length>0);
            }
            scenario.onActivity(activity->{Button stop=findButton(activity.getWindow().getDecorView(),"停止所有连接");assertNotNull(stop);stop.performClick();});
            waitFor(()->LinkService.current()==null||!LinkService.current().isRunning(),instrumentation,5000);
        }finally{
            instrumentation.runOnMainSync(()->{if(LinkService.current()!=null)LinkService.current().stopNetwork();});
        }
    }
    private static void saveScreenshot(Instrumentation instrumentation,Context context,String name){
        Bitmap screenshot=instrumentation.getUiAutomation().takeScreenshot();if(screenshot==null)return;
        try{
            File root=context.getExternalFilesDir(null);if(root==null)return;
            File directory=new File(root,"qa");if(!directory.isDirectory()&&!directory.mkdirs())return;
            try(FileOutputStream output=new FileOutputStream(new File(directory,name))){screenshot.compress(Bitmap.CompressFormat.PNG,100,output);}
        }catch(IOException ignored){
            // QA artifacts are optional and must not affect connection test success.
        }finally{screenshot.recycle();}
    }
    private interface Condition{boolean ready();}
    private static void waitFor(Condition condition,Instrumentation instrumentation,long timeout){
        long end=SystemClock.elapsedRealtime()+timeout;AtomicBoolean done=new AtomicBoolean();
        while(SystemClock.elapsedRealtime()<end){instrumentation.runOnMainSync(()->done.set(condition.ready()));if(done.get())return;SystemClock.sleep(50);}
        fail("Android connection service did not reach expected state within "+timeout+"ms");
    }
    private static Button findButton(View root,String text){
        if(root instanceof Button&&text.contentEquals(((Button)root).getText()))return(Button)root;
        if(root instanceof ViewGroup){ViewGroup group=(ViewGroup)root;for(int i=0;i<group.getChildCount();i++){Button found=findButton(group.getChildAt(i),text);if(found!=null)return found;}}
        return null;
    }
}
