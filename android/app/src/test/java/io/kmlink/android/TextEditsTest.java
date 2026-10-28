package io.kmlink.android;
import org.junit.Test;
import static org.junit.Assert.*;
public class TextEditsTest {
    @Test public void replacesSelectedTextAndKeepsCursor() {
        TextEdits.Edit edit=TextEdits.insert("hello world",6,11,"Android");
        assertEquals("hello Android",edit.text); assertEquals(13,edit.cursor);
    }
    @Test public void removesWholeSupplementaryCharacter() {
        TextEdits.Edit edit=TextEdits.backspace("a😀b",3,3);
        assertEquals("ab",edit.text); assertEquals(1,edit.cursor);
    }
    @Test public void forwardDeleteUsesCodepoints() {
        assertEquals("ab",TextEdits.delete("a😀b",1,1).text);
    }
    @Test public void clampsStaleSelection() {
        assertEquals("abcx",TextEdits.insert("abc",99,99,"x").text);
    }
}
