package io.kmlink.android;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.Locale;
import java.util.concurrent.atomic.AtomicBoolean;

/** Cancellable bounded HTTPS IO, separate from the LAN control service. */
final class UpdateClient {
    interface Progress { void update(long completed, long total); }
    interface ConnectionFactory { HttpURLConnection open(URL url) throws IOException; }
    private final ConnectionFactory connections;
    private final AtomicBoolean cancelled = new AtomicBoolean();
    private volatile HttpURLConnection active;

    UpdateClient() { this(url -> (HttpURLConnection)url.openConnection()); }
    UpdateClient(ConnectionFactory connections) { this.connections = connections; }
    void cancel() { cancelled.set(true); HttpURLConnection connection = active; if (connection != null) connection.disconnect(); }

    void checkCancelled() throws IOException {
        if (cancelled.get() || Thread.currentThread().isInterrupted()) throw new IOException("更新操作已取消");
    }

    private HttpURLConnection open(String value) throws IOException {
        String location = value;
        for (int redirects = 0; redirects <= 5; redirects++) {
            checkCancelled();
            if (!UpdateManifest.allowedHttps(location)) throw new IOException("拒绝不受信任的更新下载地址");
            URL url = new URL(location);
            HttpURLConnection connection = connections.open(url);
            active = connection;
            connection.setConnectTimeout(10_000);
            connection.setReadTimeout(20_000);
            connection.setInstanceFollowRedirects(false);
            connection.setRequestProperty("User-Agent", "KMLink-Android-Updater");
            connection.setRequestProperty("Accept", "application/vnd.github+json");
            connection.setRequestProperty("Accept-Encoding", "identity");
            try {
                int status = connection.getResponseCode();
                checkCancelled();
                if (status == 200) return connection;
                if (status == 301 || status == 302 || status == 303 || status == 307 || status == 308) {
                    String next = connection.getHeaderField("Location");
                    if (next == null) throw new IOException("更新下载重定向无效");
                    location = new URL(url, next).toExternalForm();
                    connection.disconnect(); active = null;
                    continue;
                }
                if (status == 403 || status == 429) throw new IOException("GitHub 暂时限制请求，请稍后再试");
                throw new IOException("GitHub 下载失败（HTTP " + status + "）");
            } catch (IOException | RuntimeException error) {
                connection.disconnect(); active = null; throw error;
            }
        }
        throw new IOException("更新下载重定向次数过多");
    }

    String text(String url, int limit) throws IOException {
        HttpURLConnection connection = open(url);
        try (InputStream input = connection.getInputStream(); ByteArrayOutputStream output = new ByteArrayOutputStream()) {
            long length = connection.getContentLengthLong();
            if (length > limit) throw new IOException("GitHub 响应超出大小限制");
            byte[] buffer = new byte[8192]; int count;
            while ((count = input.read(buffer)) != -1) {
                checkCancelled();
                if (output.size() + count > limit) throw new IOException("GitHub 响应超出大小限制");
                output.write(buffer, 0, count);
            }
            return new String(output.toByteArray(), StandardCharsets.UTF_8);
        } finally { connection.disconnect(); active = null; }
    }

    UpdateManifest check() throws IOException { return UpdateManifest.parse(text(UpdateManifest.API, 1024 * 1024)); }

    File download(UpdateManifest release, File directory, Progress progress) throws IOException {
        String expected = release.sha256 != null ? release.sha256
                : release.digestFromChecksums(text(release.checksumUrl, 64 * 1024));
        if (!directory.isDirectory() && !directory.mkdirs()) throw new IOException("无法创建更新缓存目录");
        // This staging name is never served by UpdateApkProvider. The caller
        // verifies APK identity/signature before publishing its final name.
        File partial = new File(directory, "kmlink-update.apk.part"), complete = new File(directory, "kmlink-download.apk");
        HttpURLConnection connection = open(release.apkUrl);
        boolean success = false;
        try (InputStream input = connection.getInputStream(); FileOutputStream output = new FileOutputStream(partial)) {
            long contentLength = connection.getContentLengthLong();
            if (contentLength >= 0 && contentLength != release.size) throw new IOException("安装包大小与发布信息不符");
            MessageDigest digest;
            try { digest = MessageDigest.getInstance("SHA-256"); }
            catch (NoSuchAlgorithmException error) { throw new IOException(error); }
            long received = 0; byte[] buffer = new byte[64 * 1024]; int count;
            while ((count = input.read(buffer)) != -1) {
                checkCancelled(); received += count;
                if (received > release.size || received > UpdateManifest.MAX_APK_BYTES) throw new IOException("安装包超出大小限制");
                output.write(buffer, 0, count); digest.update(buffer, 0, count); progress.update(received, release.size);
            }
            checkCancelled(); output.getFD().sync();
            if (received != release.size) throw new IOException("安装包下载不完整");
            if (!expected.equals(hex(digest.digest()))) throw new IOException("安装包 SHA-256 校验失败，已拒绝安装");
            success = true;
        } finally {
            connection.disconnect(); active = null;
            if (!success) partial.delete();
        }
        if (complete.exists() && !complete.delete()) { partial.delete(); throw new IOException("无法清理旧更新缓存"); }
        if (!partial.renameTo(complete)) { partial.delete(); throw new IOException("无法保存更新安装包"); }
        return complete;
    }

    static String hex(byte[] bytes) {
        StringBuilder value = new StringBuilder(bytes.length * 2);
        for (byte item : bytes) value.append(String.format(Locale.ROOT, "%02x", item & 255));
        return value.toString();
    }
}
