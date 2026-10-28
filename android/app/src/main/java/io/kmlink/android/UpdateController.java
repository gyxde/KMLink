package io.kmlink.android;

import android.app.Activity;
import android.content.ActivityNotFoundException;
import android.content.ClipData;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.net.Uri;
import android.os.Handler;
import android.os.Looper;
import android.provider.Settings;
import android.widget.Toast;
import java.io.File;
import java.io.IOException;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.function.BooleanSupplier;

/** Activity-owned update flow. LAN messages never call this controller. */
final class UpdateController {
    interface Listener { void changed(String status, String action, boolean checking, boolean canInstall); }
    private static final long CHECK_INTERVAL = 6L * 60 * 60 * 1000;
    private final Activity activity;
    private final Listener listener;
    private final Runnable beforeInstall;
    private final BooleanSupplier localActionsAllowed;
    private final Handler main = new Handler(Looper.getMainLooper());
    private final ExecutorService worker = Executors.newSingleThreadExecutor(r -> new Thread(r, "KMLink Android updates"));
    private final SharedPreferences preferences;
    private UpdateClient client;
    private UpdateManifest release;
    private File verifiedApk;
    private int generation;
    private boolean busy, closed, foreground, attemptedAutomatic, awaitingPermission;

    UpdateController(Activity activity, Listener listener, Runnable beforeInstall, BooleanSupplier localActionsAllowed) {
        this.activity = activity; this.listener = listener; this.beforeInstall = beforeInstall;
        this.localActionsAllowed = localActionsAllowed;
        preferences = activity.getSharedPreferences("kmlink-updates", Activity.MODE_PRIVATE);
        show("当前版本 " + installedVersion(activity) + " · 更新来源：GitHub gyxde/KMLink");
    }

    static String installedVersion(Activity activity) {
        try { return ApkVerifier.current(activity).versionName; }
        catch (Exception error) { return "未知"; }
    }

    void resume() {
        foreground = true;
        if (closed) return;
        if (awaitingPermission) {
            awaitingPermission = false;
            if (activity.getPackageManager().canRequestPackageInstalls()) install();
            else show("安装权限尚未开启；点击安装可重新打开设置。");
            return;
        }
        long now = System.currentTimeMillis(), previous = preferences.getLong("last-check", 0);
        if (!attemptedAutomatic && !busy && (now < previous || now - previous >= CHECK_INTERVAL)) {
            attemptedAutomatic = true; check(false);
        }
    }
    void pause() { foreground = false; }

    private void show(String message) {
        if (!closed) listener.changed(message, verifiedApk == null ? "下载并安装更新" : "安装已下载更新", busy, release != null && !busy);
    }

    void check(boolean manual) {
        if (busy || closed) return;
        busy = true; release = null; verifiedApk = null;
        int token = ++generation; client = new UpdateClient(); UpdateClient job = client;
        preferences.edit().putLong("last-check", System.currentTimeMillis()).apply();
        show("正在检查 GitHub 新版本…");
        worker.execute(() -> {
            try {
                UpdateManifest latest = job.check();
                boolean newer = UpdateManifest.compareVersions(latest.version, installedVersion(activity)) > 0;
                deliver(token, () -> {
                    busy = false; client = null;
                    if (newer) {
                        release = latest; show("发现新版本 " + latest.version + " · " + latest.size + " 字节");
                        if (foreground) Toast.makeText(activity, "发现 KMLink " + latest.version + "，可在软件更新中下载安装", Toast.LENGTH_LONG).show();
                    }
                    else show("已是最新版本（" + installedVersion(activity) + "）");
                });
            } catch (Exception error) {
                deliver(token, () -> { busy = false; client = null; show((manual ? "检查失败：" : "自动检查暂不可用：") + message(error)); });
            }
        });
    }

    void downloadOrInstall() {
        if (busy || closed || release == null) return;
        if (!localActionsAllowed.getAsBoolean()) { show("请先停止控制连接，再从本机发起更新。"); return; }
        if (verifiedApk != null) { install(); return; }
        busy = true; int token = ++generation; UpdateManifest expected = release;
        client = new UpdateClient(); UpdateClient job = client;
        show("正在下载 " + expected.version + "…");
        worker.execute(() -> {
            final long[] previous = {0}; File file = null;
            try {
                file = job.download(expected, UpdateApkProvider.directory(activity), (received, total) -> {
                    long now = System.nanoTime();
                    if (received == total || now - previous[0] > 100_000_000L) {
                        previous[0] = now;
                        deliver(token, () -> show("正在下载 " + expected.version + " · " + (received * 100 / total) + "%"));
                    }
                });
                job.checkCancelled();
                ApkVerifier.verify(activity, file, expected);
                job.checkCancelled();
                File target = new File(UpdateApkProvider.directory(activity), UpdateApkProvider.FILE_NAME);
                if ((target.exists() && !target.delete()) || !file.renameTo(target))
                    throw new IOException("无法保存已验证的安装包");
                file = target;
                File ready = file;
                deliver(token, () -> {
                    busy = false; client = null; verifiedApk = ready;
                    show("安装包已通过校验和签名验证，等待系统确认安装。");
                    if (foreground) install();
                });
            } catch (Exception error) {
                if (file != null) file.delete();
                deliver(token, () -> { busy = false; client = null; show("下载未完成：" + message(error)); });
            }
        });
    }

    private void install() {
        if (closed || busy || !foreground || verifiedApk == null || !verifiedApk.isFile()) return;
        if (!localActionsAllowed.getAsBoolean()) { show("安装包已验证。请先停止控制连接，再从本机点击安装。"); return; }
        try {
            if (!activity.getPackageManager().canRequestPackageInstalls()) {
                awaitingPermission = true;
                show("请允许 KMLink 安装更新，然后返回应用继续。");
                activity.startActivity(new Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES,
                        Uri.parse("package:" + activity.getPackageName())));
                return;
            }
            // The user pressed the update button. Stop active control before
            // the OS installer takes focus or replaces this process.
            beforeInstall.run();
            activity.startActivity(installationIntent(activity));
            show("请在 Android 系统安装器中确认更新；取消后可再次点击安装。");
        } catch (ActivityNotFoundException error) {
            awaitingPermission = false; show("此设备没有可用的系统安装器，请通过 GitHub 安装更新。");
        } catch (RuntimeException error) {
            awaitingPermission = false; show("无法打开系统安装器：" + message(error));
        }
    }

    static Intent installationIntent(Context context) {
        Uri uri = UpdateApkProvider.uri(context);
        Intent install = new Intent(Intent.ACTION_VIEW).setDataAndType(uri, "application/vnd.android.package-archive");
        install.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION);
        install.setClipData(ClipData.newRawUri("KMLink update", uri));
        return install;
    }

    void cancel() {
        if (!busy || closed) return;
        generation++; if (client != null) client.cancel(); client = null; busy = false;
        show("更新操作已取消；当前应用可继续使用。");
    }

    void close() {
        if (closed) return;
        closed = true; generation++; if (client != null) client.cancel();
        worker.shutdownNow(); main.removeCallbacksAndMessages(null);
    }
    private void deliver(int token, Runnable action) {
        main.post(() -> { if (!closed && token == generation) action.run(); });
    }
    private static String message(Exception error) {
        String value = error.getMessage(); return value == null ? "网络或安装包校验失败" : value.substring(0, Math.min(180, value.length()));
    }
}
