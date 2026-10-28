package io.kmlink.android;

import android.content.ContentProvider;
import android.content.ContentValues;
import android.content.Context;
import android.database.Cursor;
import android.database.MatrixCursor;
import android.net.Uri;
import android.os.ParcelFileDescriptor;
import android.provider.OpenableColumns;
import java.io.File;
import java.io.FileNotFoundException;

/** Grants the system installer read access to exactly one private update APK. */
public final class UpdateApkProvider extends ContentProvider {
    static final String FILE_NAME = "kmlink-update.apk";
    static File directory(Context context) { return new File(context.getFilesDir(), "updates"); }
    static Uri uri(Context context) { return new Uri.Builder().scheme("content").authority(context.getPackageName() + ".update-apk").appendPath(FILE_NAME).build(); }
    @Override public boolean onCreate() { return true; }
    private File file(Uri uri) throws FileNotFoundException {
        Context context = getContext();
        if (context == null || !"content".equals(uri.getScheme())
                || !(context.getPackageName() + ".update-apk").equals(uri.getAuthority())
                || uri.getPathSegments().size() != 1 || !FILE_NAME.equals(uri.getLastPathSegment())
                || uri.getQuery() != null || uri.getFragment() != null) throw new FileNotFoundException("Unknown update file");
        File file = new File(directory(context), FILE_NAME);
        if (!file.isFile()) throw new FileNotFoundException("Update file is unavailable");
        return file;
    }
    @Override public ParcelFileDescriptor openFile(Uri uri, String mode) throws FileNotFoundException {
        if (!"r".equals(mode)) throw new FileNotFoundException("Updates are read-only");
        return ParcelFileDescriptor.open(file(uri), ParcelFileDescriptor.MODE_READ_ONLY);
    }
    @Override public String getType(Uri uri) { return "application/vnd.android.package-archive"; }
    @Override public Cursor query(Uri uri, String[] projection, String selection, String[] args, String order) {
        try {
            File file = file(uri);
            String[] columns = projection == null ? new String[]{OpenableColumns.DISPLAY_NAME, OpenableColumns.SIZE} : projection;
            MatrixCursor cursor = new MatrixCursor(columns, 1); Object[] row = new Object[columns.length];
            for (int i = 0; i < columns.length; i++) {
                if (OpenableColumns.DISPLAY_NAME.equals(columns[i])) row[i] = FILE_NAME;
                else if (OpenableColumns.SIZE.equals(columns[i])) row[i] = file.length();
            }
            cursor.addRow(row); return cursor;
        } catch (FileNotFoundException error) { throw new IllegalArgumentException(error); }
    }
    @Override public Uri insert(Uri uri, ContentValues values) { throw new SecurityException("Read-only update provider"); }
    @Override public int delete(Uri uri, String selection, String[] args) { throw new SecurityException("Read-only update provider"); }
    @Override public int update(Uri uri, ContentValues values, String selection, String[] args) { throw new SecurityException("Read-only update provider"); }
}
