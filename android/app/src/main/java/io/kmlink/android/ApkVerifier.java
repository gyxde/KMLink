package io.kmlink.android;

import android.content.Context;
import android.content.pm.ApplicationInfo;
import android.content.pm.PackageInfo;
import android.content.pm.PackageManager;
import android.content.pm.Signature;
import android.os.Build;
import java.io.File;
import java.io.IOException;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;

/** Rejects wrong apps, downgrades, debug packages and changed signing identities. */
final class ApkVerifier {
    static PackageInfo current(Context context) throws PackageManager.NameNotFoundException {
        return context.getPackageManager().getPackageInfo(context.getPackageName(), flags());
    }
    private static int flags() { return Build.VERSION.SDK_INT >= 28 ? PackageManager.GET_SIGNING_CERTIFICATES : PackageManager.GET_SIGNATURES; }
    private static long versionCode(PackageInfo info) { return Build.VERSION.SDK_INT >= 28 ? info.getLongVersionCode() : info.versionCode; }
    @SuppressWarnings("deprecation")
    private static List<byte[]> certificates(PackageInfo info) {
        Signature[] signatures = Build.VERSION.SDK_INT >= 28 && info.signingInfo != null
                ? info.signingInfo.getApkContentsSigners() : info.signatures;
        List<byte[]> result = new ArrayList<>();
        if (signatures != null) for (Signature signature : signatures) result.add(signature.toByteArray());
        return result;
    }
    static boolean sameCertificates(List<byte[]> left, List<byte[]> right) {
        if (left.isEmpty() || left.size() != right.size()) return false;
        List<String> a = new ArrayList<>(), b = new ArrayList<>();
        for (byte[] cert : left) a.add(UpdateClient.hex(cert));
        for (byte[] cert : right) b.add(UpdateClient.hex(cert));
        Collections.sort(a); Collections.sort(b);
        return a.equals(b);
    }
    static void verify(Context context, File file, UpdateManifest release) throws IOException {
        PackageManager manager = context.getPackageManager();
        PackageInfo archive = manager.getPackageArchiveInfo(file.getAbsolutePath(), flags());
        PackageInfo installed;
        try { installed = current(context); }
        catch (PackageManager.NameNotFoundException error) { throw new IOException("无法读取当前应用签名", error); }
        if (archive == null || !context.getPackageName().equals(archive.packageName)) throw new IOException("安装包不是 KMLink，已拒绝安装");
        if (archive.applicationInfo == null || (archive.applicationInfo.flags & ApplicationInfo.FLAG_DEBUGGABLE) != 0)
            throw new IOException("拒绝安装调试版本");
        if (!release.version.equals(archive.versionName) || versionCode(archive) <= versionCode(installed))
            throw new IOException("安装包版本不匹配或不是更新版本");
        if (!sameCertificates(certificates(installed), certificates(archive)))
            throw new IOException("安装包签名与当前应用不同，已拒绝安装；请使用正式版更新正式版");
    }
}
