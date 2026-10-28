package io.kmlink.android;

import android.accessibilityservice.AccessibilityService;
import android.accessibilityservice.GestureDescription;
import android.app.KeyguardManager;
import android.content.Context;
import android.content.Intent;
import android.graphics.Canvas;
import android.graphics.Color;
import android.graphics.Paint;
import android.graphics.Path;
import android.os.Build;
import android.os.Bundle;
import android.view.Gravity;
import android.view.View;
import android.view.WindowManager;
import android.view.accessibility.AccessibilityEvent;
import android.view.accessibility.AccessibilityNodeInfo;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.List;

/** Uses Android's user-enabled accessibility APIs, never privileged input injection. */
public final class RemoteAccessibilityService extends AccessibilityService {
    private static volatile RemoteAccessibilityService active;
    private WindowManager windows;
    private CursorView cursor;
    private WindowManager.LayoutParams cursorLayout;
    private float x, y;
    private boolean leftDown, ctrl, shift;
    private final List<float[]> drag = new ArrayList<>();
    private final ArrayDeque<GestureDescription> gestures = new ArrayDeque<>();
    private boolean dispatching;
    private int generation;

    public static RemoteAccessibilityService current() { return active; }
    public static boolean available() { return active != null; }
    public boolean isUnlocked() {
        KeyguardManager keyguard = (KeyguardManager) getSystemService(Context.KEYGUARD_SERVICE);
        return keyguard != null && !keyguard.isKeyguardLocked();
    }

    @Override protected void onServiceConnected() {
        super.onServiceConnected(); active = this;
        windows = (WindowManager) getSystemService(WINDOW_SERVICE);
        LinkService service = LinkService.current(); if (service != null) service.accessibilityChanged();
    }
    @Override public void onAccessibilityEvent(AccessibilityEvent event) {
        if (!isUnlocked()) {
            resetInput();
            LinkService service = LinkService.current(); if (service != null) service.locked();
        }
    }
    @Override public void onInterrupt() { resetInput(); }
    @Override public boolean onUnbind(Intent intent) { shutdown(); return super.onUnbind(intent); }
    @Override public void onDestroy() { shutdown(); super.onDestroy(); }
    private void shutdown() {
        resetInput(); if (active == this) active = null;
        LinkService service = LinkService.current(); if (service != null) service.accessibilityChanged();
    }
    private void problem(String message) {
        LinkService service = LinkService.current(); if (service != null) service.reportInputProblem(message);
    }
    public void moveCursor(float nextX, float nextY) {
        x = nextX; y = nextY;
        if (!isUnlocked()) { resetInput(); return; }
        if (cursor == null) {
            cursor = new CursorView(this);
            int size = Math.round(30 * getResources().getDisplayMetrics().density);
            cursorLayout = new WindowManager.LayoutParams(size, size,
                    WindowManager.LayoutParams.TYPE_ACCESSIBILITY_OVERLAY,
                    WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE | WindowManager.LayoutParams.FLAG_NOT_TOUCHABLE
                            | WindowManager.LayoutParams.FLAG_LAYOUT_IN_SCREEN | WindowManager.LayoutParams.FLAG_LAYOUT_NO_LIMITS
                            | WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON,
                    android.graphics.PixelFormat.TRANSLUCENT);
            cursorLayout.gravity = Gravity.TOP | Gravity.LEFT;
            try { windows.addView(cursor, cursorLayout); }
            catch (RuntimeException error) { cursor = null; problem("无法显示控制光标，请重新启用辅助功能。"); return; }
        }
        cursorLayout.x = Math.round(x); cursorLayout.y = Math.round(y);
        try { windows.updateViewLayout(cursor, cursorLayout); } catch (RuntimeException ignored) {}
        if (leftDown && drag.size() < 256) drag.add(new float[]{x, y});
    }
    public void resetInput() {
        leftDown = ctrl = shift = false; drag.clear(); gestures.clear(); generation++;
        dispatching = false;
        if (cursor != null && windows != null) {
            try { windows.removeView(cursor); } catch (RuntimeException ignored) {}
        }
        cursor = null;
    }
    public void button(String name, boolean pressed) {
        if (!isUnlocked()) return;
        if ("left".equals(name)) {
            if (pressed) {
                if (leftDown) return;
                leftDown = true; drag.clear(); drag.add(new float[]{x,y});
            } else if (leftDown) {
                leftDown = false;
                Path path = new Path();
                float[] first = drag.isEmpty() ? new float[]{x,y} : drag.get(0);
                path.moveTo(first[0], first[1]);
                for (int i=1; i<drag.size(); i++) path.lineTo(drag.get(i)[0], drag.get(i)[1]);
                path.lineTo(x,y);
                double distance = Math.hypot(x-first[0], y-first[1]);
                gesture(path, distance < 4 ? 50 : 300); drag.clear();
            }
        } else if (!pressed && "right".equals(name)) {
            performGlobalAction(GLOBAL_ACTION_BACK);
        } else if (!pressed && "middle".equals(name)) {
            performGlobalAction(GLOBAL_ACTION_HOME);
        }
    }
    public void scroll(float dx, float dy, int width, int height) {
        if (!isUnlocked() || (dx == 0 && dy == 0)) return;
        float endX = Math.max(2, Math.min(width-3, x - dx*55));
        float endY = Math.max(2, Math.min(height-3, y + dy*55));
        Path path = new Path(); path.moveTo(x,y); path.lineTo(endX,endY);
        gesture(path, 170);
    }
    private void gesture(Path path, long duration) {
        if (!isUnlocked()) return;
        GestureDescription gesture = new GestureDescription.Builder()
                .addStroke(new GestureDescription.StrokeDescription(path, 0, duration)).build();
        if (gestures.size() >= 8) gestures.removeFirst();
        gestures.addLast(gesture); dispatchNext();
    }
    private void dispatchNext() {
        if (dispatching || gestures.isEmpty() || !isUnlocked()) return;
        dispatching = true; final int token = generation;
        boolean accepted = dispatchGesture(gestures.removeFirst(), new GestureResultCallback() {
            private void done() { if (token == generation) { dispatching = false; dispatchNext(); } }
            @Override public void onCompleted(GestureDescription description) { done(); }
            @Override public void onCancelled(GestureDescription description) { done(); }
        }, null);
        if (!accepted) { dispatching = false; problem("系统未接受触摸动作；请确认辅助功能仍已开启。"); }
    }
    private AccessibilityNodeInfo editable() {
        if (!isUnlocked()) return null;
        AccessibilityNodeInfo root = getRootInActiveWindow();
        if (root == null) return null;
        AccessibilityNodeInfo node = root.findFocus(AccessibilityNodeInfo.FOCUS_INPUT);
        if (root != node) root.recycle();
        if (node == null) { problem("请先点击目标应用中的输入框。"); return null; }
        if (!node.isEditable() || node.isPassword()) {
            node.recycle(); problem("此输入框无法远程编辑；密码和安全输入框请在手机上操作。"); return null;
        }
        return node;
    }
    public void insertText(String value) {
        AccessibilityNodeInfo node = editable(); if (node == null) return;
        try {
            String original = node.getText() == null ? "" : node.getText().toString();
            int start = node.getTextSelectionStart(), end = node.getTextSelectionEnd();
            if (start < 0 || end < 0) start = end = original.length();
            apply(node, TextEdits.insert(original,start,end,value));
        } finally { node.recycle(); }
    }
    private void apply(AccessibilityNodeInfo node, TextEdits.Edit edit) {
        Bundle text = new Bundle(); text.putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE,edit.text);
        if (!node.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT,text)) { problem("目标应用不支持辅助功能文字输入。"); return; }
        Bundle select = new Bundle();
        select.putInt(AccessibilityNodeInfo.ACTION_ARGUMENT_SELECTION_START_INT,edit.cursor);
        select.putInt(AccessibilityNodeInfo.ACTION_ARGUMENT_SELECTION_END_INT,edit.cursor);
        node.performAction(AccessibilityNodeInfo.ACTION_SET_SELECTION,select);
    }
    public void key(String code, boolean pressed) {
        if (code.startsWith("named:ctrl")) { ctrl = pressed; return; }
        if (code.startsWith("named:shift")) { shift = pressed; return; }
        if (!pressed || !isUnlocked()) return;
        if ("named:esc".equals(code)) { performGlobalAction(GLOBAL_ACTION_BACK); return; }
        if (code.startsWith("char:")) {
            String value = code.substring(5);
            if (ctrl) { shortcut(value); return; }
            if (shift && value.length()==1) {
                String plain="`1234567890-=[]\\;',./";String shifted="~!@#$%^&*()_+{}|:\"<>?";
                int index=plain.indexOf(value.charAt(0));value=index>=0?String.valueOf(shifted.charAt(index)):value.toUpperCase(java.util.Locale.ROOT);
            }
            insertText(value); return;
        }
        if ("named:space".equals(code)) { insertText(" "); return; }
        if ("named:tab".equals(code)) {
            AccessibilityNodeInfo node=editable(); if(node==null)return;
            AccessibilityNodeInfo next=node.focusSearch(shift?View.FOCUS_BACKWARD:View.FOCUS_FORWARD);
            if(next!=null){next.performAction(AccessibilityNodeInfo.ACTION_FOCUS);next.recycle();}
            node.recycle(); return;
        }
        AccessibilityNodeInfo node=editable(); if(node==null)return;
        try {
            String text=node.getText()==null?"":node.getText().toString();
            int start=node.getTextSelectionStart(),end=node.getTextSelectionEnd();
            if(start<0||end<0)start=end=text.length();
            if("named:backspace".equals(code)) apply(node,TextEdits.backspace(text,start,end));
            else if("named:delete".equals(code))apply(node,TextEdits.delete(text,start,end));
            else if("named:enter".equals(code)) {
                if (Build.VERSION.SDK_INT >= 30 && node.performAction(AccessibilityNodeInfo.AccessibilityAction.ACTION_IME_ENTER.getId())) return;
                if(node.isMultiLine())apply(node,TextEdits.insert(text,start,end,"\n"));
            } else {
                int position=end;
                if("named:left".equals(code)&&position>0)position=text.offsetByCodePoints(position,-1);
                else if("named:right".equals(code)&&position<text.length())position=text.offsetByCodePoints(position,1);
                else if("named:home".equals(code))position=0;
                else if("named:end".equals(code))position=text.length();
                else return;
                Bundle selection=new Bundle();
                selection.putInt(AccessibilityNodeInfo.ACTION_ARGUMENT_SELECTION_START_INT,shift?start:position);
                selection.putInt(AccessibilityNodeInfo.ACTION_ARGUMENT_SELECTION_END_INT,position);
                node.performAction(AccessibilityNodeInfo.ACTION_SET_SELECTION,selection);
            }
        } finally {node.recycle();}
    }
    private void shortcut(String character) {
        AccessibilityNodeInfo node=editable(); if(node==null)return;
        try {
            switch(character.toLowerCase(java.util.Locale.ROOT)) {
                case "a":
                    Bundle selection=new Bundle(); selection.putInt(AccessibilityNodeInfo.ACTION_ARGUMENT_SELECTION_START_INT,0);
                    selection.putInt(AccessibilityNodeInfo.ACTION_ARGUMENT_SELECTION_END_INT,node.getText()==null?0:node.getText().length());
                    node.performAction(AccessibilityNodeInfo.ACTION_SET_SELECTION,selection); break;
                case "c": node.performAction(AccessibilityNodeInfo.ACTION_COPY); break;
                case "x": node.performAction(AccessibilityNodeInfo.ACTION_CUT); break;
                case "v": node.performAction(AccessibilityNodeInfo.ACTION_PASTE); break;
                default: break;
            }
        } finally {node.recycle();}
    }
    private static final class CursorView extends View {
        private final Paint paint=new Paint(Paint.ANTI_ALIAS_FLAG);
        CursorView(Context context) {super(context);setImportantForAccessibility(View.IMPORTANT_FOR_ACCESSIBILITY_NO);}
        @Override protected void onDraw(Canvas canvas) {
            super.onDraw(canvas); float density=getResources().getDisplayMetrics().density;
            canvas.save();canvas.scale(density,density);
            Path arrow=new Path();arrow.moveTo(1,1);arrow.lineTo(2,22);arrow.lineTo(8,16);arrow.lineTo(13,26);arrow.lineTo(17,24);arrow.lineTo(12,14);arrow.lineTo(21,14);arrow.close();
            paint.setColor(Color.WHITE);paint.setStyle(Paint.Style.FILL);canvas.drawPath(arrow,paint);
            paint.setColor(Color.rgb(22,124,113));paint.setStrokeWidth(1.8f);paint.setStyle(Paint.Style.STROKE);canvas.drawPath(arrow,paint);
            canvas.restore();
        }
    }
}
