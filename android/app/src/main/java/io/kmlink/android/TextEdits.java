package io.kmlink.android;

/** Pure text operations shared by accessibility input and JVM regression tests. */
final class TextEdits {
    static final class Edit {
        final String text; final int cursor;
        Edit(String text, int cursor) { this.text = text; this.cursor = cursor; }
    }
    static Edit insert(String text, int start, int end, String value) {
        int a = Math.max(0, Math.min(text.length(), Math.min(start, end)));
        int b = Math.max(a, Math.min(text.length(), Math.max(start, end)));
        return new Edit(text.substring(0, a) + value + text.substring(b), a + value.length());
    }
    static Edit backspace(String text, int start, int end) {
        int a = Math.max(0, Math.min(text.length(), Math.min(start, end)));
        int b = Math.max(a, Math.min(text.length(), Math.max(start, end)));
        if (a == b && a > 0) a = text.offsetByCodePoints(a, -1);
        return insert(text, a, b, "");
    }
    static Edit delete(String text, int start, int end) {
        int a = Math.max(0, Math.min(text.length(), Math.min(start, end)));
        int b = Math.max(a, Math.min(text.length(), Math.max(start, end)));
        if (a == b && b < text.length()) b = text.offsetByCodePoints(b, 1);
        return insert(text, a, b, "");
    }
    private TextEdits() {}
}
