package io.kmlink.android;

import java.io.IOException;
import java.util.Arrays;
import java.util.Collections;
import org.json.JSONArray;
import org.json.JSONObject;
import org.junit.Test;
import static org.junit.Assert.*;

public final class UpdateManifestTest {
    static String release(String version, long size, String digest) throws Exception {
        JSONObject apk = new JSONObject().put("name", UpdateManifest.APK_NAME).put("state", "uploaded")
                .put("browser_download_url", "https://github.com/gyxde/KMLink/releases/download/v" + version + "/KMLink-android.apk")
                .put("size", size);
        if (digest != null) apk.put("digest", digest);
        return new JSONObject().put("tag_name", "v" + version).put("draft", false).put("prerelease", false)
                .put("assets", new JSONArray().put(apk)).toString();
    }
    @Test public void comparesSemanticVersionsWithoutLexicalMistakes() throws Exception {
        assertTrue(UpdateManifest.compareVersions("0.10.0", "0.9.9") > 0);
        assertEquals(0, UpdateManifest.compareVersions("v1.2.3", "1.2.3"));
        assertTrue(UpdateManifest.compareVersions("2.0.0", "1.99.99") > 0);
        for (String invalid : Arrays.asList("1.2", "1.2.3-beta", "01.2.3", "1.2.3/../", "-1.2.3")) {
            try { UpdateManifest.compareVersions(invalid, "1.2.3"); fail(invalid); }
            catch (IOException expected) {}
        }
    }
    @Test public void onlyAcceptsTheMatchingStableRepositoryApk() throws Exception {
        String sha = String.join("", Collections.nCopies(64, "a"));
        UpdateManifest manifest = UpdateManifest.parse(release("0.4.0", 12345, "sha256:" + sha));
        assertEquals("0.4.0", manifest.version); assertEquals(sha, manifest.sha256);
        String valid = release("0.4.0", 12345, "sha256:" + sha);
        for (String invalid : Arrays.asList(valid.replace("gyxde/KMLink", "attacker/KMLink"),
                valid.replace("\"draft\":false", "\"draft\":true"),
                valid.replace("\"prerelease\":false", "\"prerelease\":true"),
                valid.replace("sha256:" + sha, "sha256:bad"),
                release("0.4.0", UpdateManifest.MAX_APK_BYTES + 1, null))) {
            try { UpdateManifest.parse(invalid); fail(invalid); }
            catch (IOException expected) {}
        }
    }
    @Test public void rejectsDuplicateApkNames() throws Exception {
        JSONObject value = new JSONObject(release("0.4.0", 12345, null));
        JSONArray assets = value.getJSONArray("assets"); assets.put(assets.getJSONObject(0));
        try { UpdateManifest.parse(value.toString()); fail(); } catch (IOException expected) {}
    }
    @Test public void checksumFallbackRequiresExactlyOneValidEntry() throws Exception {
        UpdateManifest manifest = UpdateManifest.parse(release("0.4.0", 123, null));
        String sha = String.join("", Collections.nCopies(64, "b"));
        assertEquals(sha, manifest.digestFromChecksums(sha + "  KMLink-android.apk\n"));
        assertEquals(sha, manifest.digestFromChecksums(sha + " *KMLink-android.apk\n"));
        for (String invalid : Arrays.asList(sha + "  another.apk", "bad KMLink-android.apk", sha + " KMLink-android.apk\n" + sha + " KMLink-android.apk")) {
            try { manifest.digestFromChecksums(invalid); fail(); } catch (IOException expected) {}
        }
    }
    @Test public void urlPolicyAllowsOnlyGithubHttpsHosts() {
        assertTrue(UpdateManifest.allowedHttps("https://release-assets.githubusercontent.com/path?sig=abc"));
        assertTrue(UpdateManifest.allowedHttps(UpdateManifest.API));
        for (String invalid : Arrays.asList("http://github.com/a", "https://github.com.attacker.test/a", "https://user@github.com/a",
                "https://127.0.0.1/a", "https://github.com:8443/a", "https://github.com/a#b", "file:///tmp/app.apk"))
            assertFalse(invalid, UpdateManifest.allowedHttps(invalid));
    }
    @Test public void signaturesMustMatchIncludingEverySigner() {
        assertTrue(ApkVerifier.sameCertificates(Arrays.asList(new byte[]{1}, new byte[]{2}), Arrays.asList(new byte[]{2}, new byte[]{1})));
        assertFalse(ApkVerifier.sameCertificates(Arrays.asList(new byte[]{1}), Arrays.asList(new byte[]{2})));
        assertFalse(ApkVerifier.sameCertificates(Collections.emptyList(), Collections.emptyList()));
        assertFalse(ApkVerifier.sameCertificates(Arrays.asList(new byte[]{1}), Arrays.asList(new byte[]{1}, new byte[]{2})));
    }
}
