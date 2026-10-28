package io.kmlink.android;

import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.content.pm.ProviderInfo;
import android.net.Uri;
import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.platform.app.InstrumentationRegistry;
import java.io.File;
import java.io.FileNotFoundException;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import org.junit.Test;
import org.junit.runner.RunWith;
import static org.junit.Assert.*;

@RunWith(AndroidJUnit4.class)
public final class UpdateSafetyTest {
    @Test public void installerIntentGrantsOnlyTheVerifiedContentUriReadAccess() {
        Context context=InstrumentationRegistry.getInstrumentation().getTargetContext();
        Intent install=UpdateController.installationIntent(context);
        assertEquals(Intent.ACTION_VIEW,install.getAction());
        assertEquals("application/vnd.android.package-archive",install.getType());
        assertEquals(UpdateApkProvider.uri(context),install.getData());
        assertTrue((install.getFlags()&Intent.FLAG_GRANT_READ_URI_PERMISSION)!=0);
        assertEquals(0,install.getFlags()&Intent.FLAG_GRANT_WRITE_URI_PERMISSION);
        assertEquals(1,install.getClipData().getItemCount());
        assertEquals(install.getData(),install.getClipData().getItemAt(0).getUri());
    }
    @Test public void updateProviderIsPrivateReadOnlyAndLimitedToOneFile() throws Exception {
        Context context=InstrumentationRegistry.getInstrumentation().getTargetContext();
        ProviderInfo info=context.getPackageManager().getProviderInfo(new ComponentName(context,UpdateApkProvider.class),0);
        assertFalse(info.exported);assertTrue(info.grantUriPermissions);
        File directory=UpdateApkProvider.directory(context);assertTrue(directory.isDirectory()||directory.mkdirs());
        File file=new File(directory,UpdateApkProvider.FILE_NAME);
        try {
            try(FileOutputStream output=new FileOutputStream(file)){output.write("fixture".getBytes(StandardCharsets.UTF_8));}
            Uri uri=UpdateApkProvider.uri(context);
            try(InputStream input=context.getContentResolver().openInputStream(uri)){assertEquals('f',input.read());}
            for(Uri denied:new Uri[]{uri.buildUpon().path("/other.apk").build(),uri.buildUpon().path("/../kmlink-update.apk").build()}) {
                try{context.getContentResolver().openInputStream(denied);fail();}catch(FileNotFoundException expected){}
            }
            try{context.getContentResolver().openFileDescriptor(uri,"rw");fail();}catch(FileNotFoundException expected){}
        } finally {file.delete();}
    }
    @Test public void installedPackageCannotBeReinstalledAsAnUnverifiedNewVersion() throws Exception {
        Context context=InstrumentationRegistry.getInstrumentation().getTargetContext();
        String body="{\"tag_name\":\"v9999.0.0\",\"draft\":false,\"prerelease\":false,\"assets\":[{\"name\":\"KMLink-android.apk\",\"state\":\"uploaded\",\"size\":1,\"browser_download_url\":\"https://github.com/gyxde/KMLink/releases/download/v9999.0.0/KMLink-android.apk\"}]}";
        try {ApkVerifier.verify(context,new File(context.getApplicationInfo().sourceDir),UpdateManifest.parse(body));fail();}
        catch(IOException expected){}
    }
}
