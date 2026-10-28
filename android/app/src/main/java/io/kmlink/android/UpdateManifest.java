package io.kmlink.android;

import java.io.IOException;
import java.net.URI;
import java.net.URISyntaxException;
import java.util.Locale;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

/** Fixed-repository release metadata. No remote URL can choose an arbitrary APK. */
final class UpdateManifest {
    static final String REPOSITORY = "gyxde/KMLink";
    static final String API = "https://api.github.com/repos/" + REPOSITORY + "/releases/latest";
    static final String APK_NAME = "KMLink-android.apk";
    static final long MAX_APK_BYTES = 256L * 1024 * 1024;
    private static final Pattern VERSION = Pattern.compile("(?:v)?(0|[1-9][0-9]{0,8})\\.(0|[1-9][0-9]{0,8})\\.(0|[1-9][0-9]{0,8})");
    private static final Pattern SHA256 = Pattern.compile("[0-9a-fA-F]{64}");
    final String version, tag, apkUrl, checksumUrl, sha256;
    final long size;

    private UpdateManifest(String tag, String apkUrl, long size, String sha256) {
        this.tag = tag;
        this.version = tag.substring(1);
        this.apkUrl = apkUrl;
        this.size = size;
        this.sha256 = sha256;
        this.checksumUrl = "https://github.com/" + REPOSITORY + "/releases/download/" + tag + "/SHA256SUMS.txt";
    }

    static int compareVersions(String left, String right) throws IOException {
        long[] a = versionParts(left), b = versionParts(right);
        for (int i = 0; i < a.length; i++) if (a[i] != b[i]) return Long.compare(a[i], b[i]);
        return 0;
    }

    private static long[] versionParts(String value) throws IOException {
        if (value == null) throw new IOException("版本号缺失");
        Matcher match = VERSION.matcher(value);
        if (!match.matches()) throw new IOException("版本号格式无效");
        return new long[]{Long.parseLong(match.group(1)), Long.parseLong(match.group(2)), Long.parseLong(match.group(3))};
    }

    static UpdateManifest parse(String source) throws IOException {
        try {
            JSONObject release = new JSONObject(source);
            if (!Boolean.FALSE.equals(release.opt("draft")) || !Boolean.FALSE.equals(release.opt("prerelease")))
                throw new IOException("仅接受正式发布版本");
            Object value = release.opt("tag_name");
            if (!(value instanceof String) || !((String)value).startsWith("v")) throw new IOException("发布标签无效");
            String tag = (String)value;
            versionParts(tag);
            String expectedUrl = "https://github.com/" + REPOSITORY + "/releases/download/" + tag + "/" + APK_NAME;
            JSONArray assets = release.getJSONArray("assets");
            JSONObject found = null;
            for (int i = 0; i < assets.length(); i++) {
                JSONObject asset = assets.getJSONObject(i);
                if (APK_NAME.equals(asset.opt("name"))) {
                    if (found != null) throw new IOException("发布包含重复的 Android 安装包");
                    found = asset;
                }
            }
            if (found == null || !"uploaded".equals(found.opt("state"))) throw new IOException("此版本尚无 Android 安装包");
            if (!expectedUrl.equals(found.opt("browser_download_url"))) throw new IOException("Android 安装包来源不匹配");
            Object bytes = found.opt("size");
            if (!(bytes instanceof Number) || bytes instanceof Double || bytes instanceof Float)
                throw new IOException("安装包大小无效");
            long size = ((Number)bytes).longValue();
            if (size <= 0 || size > MAX_APK_BYTES) throw new IOException("安装包大小超出限制");
            String digest = null;
            Object rawDigest = found.opt("digest");
            if (rawDigest instanceof String && !((String)rawDigest).isEmpty()) {
                String raw = (String)rawDigest;
                if (!raw.startsWith("sha256:") || !SHA256.matcher(raw.substring(7)).matches())
                    throw new IOException("安装包校验信息无效");
                digest = raw.substring(7).toLowerCase(Locale.ROOT);
            }
            return new UpdateManifest(tag, expectedUrl, size, digest);
        } catch (JSONException | IllegalArgumentException error) {
            throw new IOException("GitHub 发布信息无效", error);
        }
    }

    String digestFromChecksums(String text) throws IOException {
        String found = null;
        for (String line : text.split("\\r?\\n")) {
            String[] parts = line.trim().split("\\s+", 2);
            if (parts.length == 2 && APK_NAME.equals(parts[1].replaceFirst("^\\*", ""))) {
                if (found != null || !SHA256.matcher(parts[0]).matches()) throw new IOException("校验清单中的 Android 项无效");
                found = parts[0].toLowerCase(Locale.ROOT);
            }
        }
        if (found == null) throw new IOException("校验清单未包含 Android 安装包");
        return found;
    }

    static boolean allowedHttps(String value) {
        try {
            URI uri = new URI(value);
            String host = uri.getHost();
            return "https".equals(uri.getScheme()) && uri.getRawUserInfo() == null
                    && (uri.getPort() == -1 || uri.getPort() == 443) && uri.getRawFragment() == null
                    && ("github.com".equals(host) || "api.github.com".equals(host)
                    || "release-assets.githubusercontent.com".equals(host)
                    || "objects.githubusercontent.com".equals(host)
                    || "github-releases.githubusercontent.com".equals(host));
        } catch (URISyntaxException | NullPointerException error) { return false; }
    }
}
