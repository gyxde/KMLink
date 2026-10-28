package io.kmlink.android;
import android.content.Context;
import android.graphics.Canvas;
import android.graphics.Color;
import android.graphics.Paint;
import android.graphics.RectF;
import android.view.GestureDetector;
import android.view.MotionEvent;
import android.view.View;

/** Input remains scoped to this view and the visible activity. */
final class TouchpadView extends View {
    interface Sink { void move(float dx,float dy); void scroll(float dx,float dy); void click(); void drag(boolean down); }
    private final Paint paint=new Paint(Paint.ANTI_ALIAS_FLAG);
    private final GestureDetector detector;
    private Sink sink;
    private boolean active,dragging,twoFingers;
    private float lastX,lastY,startX,startY;
    private long started;
    TouchpadView(Context context){
        super(context);setFocusable(true);setFocusableInTouchMode(true);
        setContentDescription("远程电脑触控板：单指移动，轻点左键，双指滚动，长按后拖动");
        detector=new GestureDetector(context,new GestureDetector.SimpleOnGestureListener(){
            @Override public boolean onDown(MotionEvent event){return true;}
            @Override public void onLongPress(MotionEvent event){if(active&&!twoFingers&&sink!=null){dragging=true;sink.drag(true);performHapticFeedback(android.view.HapticFeedbackConstants.LONG_PRESS);}}
        });
    }
    void setSink(Sink sink){this.sink=sink;}
    void setActive(boolean value){active=value;invalidate();}
    @Override protected void onDraw(Canvas c){
        super.onDraw(c);float d=getResources().getDisplayMetrics().density;
        paint.setStyle(Paint.Style.FILL);paint.setColor(active?Color.rgb(225,244,238):Color.rgb(239,243,245));
        c.drawRoundRect(new RectF(0,0,getWidth(),getHeight()),14*d,14*d,paint);
        paint.setStyle(Paint.Style.STROKE);paint.setStrokeWidth(d);paint.setColor(Color.rgb(191,214,210));
        c.drawRoundRect(new RectF(d,d,getWidth()-d,getHeight()-d),14*d,14*d,paint);
        paint.setStyle(Paint.Style.FILL);paint.setColor(Color.rgb(63,102,100));paint.setTextAlign(Paint.Align.CENTER);paint.setTextSize(16*d);
        c.drawText(active?"在这里滑动，移动电脑光标":"连接后点击「开始控制」",getWidth()/2f,getHeight()/2f-8*d,paint);
        paint.setTextSize(12*d);c.drawText("轻点左键 · 双指滚动 · 长按拖动",getWidth()/2f,getHeight()/2f+18*d,paint);
    }
    @Override public boolean onTouchEvent(MotionEvent event){
        if(!active)return false;
        if(event.getActionMasked()==MotionEvent.ACTION_DOWN){requestFocus();getParent().requestDisallowInterceptTouchEvent(true);}
        detector.onTouchEvent(event);
        float x=0,y=0;for(int i=0;i<event.getPointerCount();i++){x+=event.getX(i);y+=event.getY(i);}x/=event.getPointerCount();y/=event.getPointerCount();
        switch(event.getActionMasked()){
            case MotionEvent.ACTION_DOWN:lastX=startX=x;lastY=startY=y;started=event.getEventTime();twoFingers=false;dragging=false;break;
            case MotionEvent.ACTION_POINTER_DOWN:twoFingers=true;if(dragging&&sink!=null)sink.drag(false);dragging=false;lastX=x;lastY=y;break;
            case MotionEvent.ACTION_POINTER_UP:lastX=Float.NaN;lastY=Float.NaN;break;
            case MotionEvent.ACTION_MOVE:
                if(!Float.isNaN(lastX)&&sink!=null){if(event.getPointerCount()>=2)sink.scroll(-(x-lastX)/24f,-(y-lastY)/24f);else sink.move((x-lastX)*1.6f,(y-lastY)*1.6f);}
                lastX=x;lastY=y;break;
            case MotionEvent.ACTION_UP:
                if(sink!=null){if(dragging)sink.drag(false);else if(!twoFingers&&Math.hypot(x-startX,y-startY)<12*getResources().getDisplayMetrics().density&&event.getEventTime()-started<450)sink.click();}
                dragging=false;getParent().requestDisallowInterceptTouchEvent(false);performClick();break;
            case MotionEvent.ACTION_CANCEL:if(dragging&&sink!=null)sink.drag(false);dragging=false;getParent().requestDisallowInterceptTouchEvent(false);break;
        }
        return true;
    }
    @Override public boolean performClick(){super.performClick();return true;}
}
