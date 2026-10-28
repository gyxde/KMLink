package io.kmlink.android;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.res.Configuration;
import android.content.pm.ServiceInfo;
import android.graphics.Point;
import android.net.wifi.WifiManager;
import android.os.Binder;
import android.os.Build;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.util.DisplayMetrics;
import android.view.WindowManager;
import io.kmlink.protocol.KMLinkNode;
import io.kmlink.protocol.Peer;
import io.kmlink.protocol.PendingRequest;
import org.json.JSONArray;
import org.json.JSONObject;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.CopyOnWriteArraySet;
import java.util.concurrent.ArrayBlockingQueue;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/** User-started, visible connection service. No screen capture or background clipboard reads. */
public final class LinkService extends Service {
    public static final String ACTION_START="io.kmlink.android.START";
    public static final String ACTION_STOP="io.kmlink.android.STOP";
    private static final String CHANNEL="kmlink-connection", REQUEST_CHANNEL="kmlink-request";
    private static final int NOTIFICATION_ID=2401, REQUEST_ID=2402;
    private static volatile LinkService active;
    public interface Observer { void onStateChanged(); }
    public final class LocalBinder extends Binder { public LinkService service(){return LinkService.this;} }
    private final IBinder binder=new LocalBinder();
    private final Handler main=new Handler(Looper.getMainLooper());
    private final ExecutorService worker=Executors.newSingleThreadExecutor();
    private final Set<Observer> observers=new CopyOnWriteArraySet<>();
    private volatile KMLinkNode node;
    private static final class Packet { final int token,link; final JSONObject value; Packet(int token,int link,JSONObject value){this.token=token;this.link=link;this.value=value;} }
    private final AtomicInteger linkGeneration=new AtomicInteger();
    private final ArrayBlockingQueue<Packet> incoming=new ArrayBlockingQueue<>(1024);
    private final AtomicBoolean drainScheduled=new AtomicBoolean();
    private final Runnable drainInput=new Runnable(){public void run(){
        for(int i=0;i<128;i++){Packet packet=incoming.poll();if(packet==null)break;if(packet.token==generation&&packet.link==linkGeneration.get())handleMessage(packet.value);}
        if(!incoming.isEmpty())main.postDelayed(this,8);
        else{drainScheduled.set(false);if(!incoming.isEmpty()&&drainScheduled.compareAndSet(false,true))main.post(this);}
    }};
    private volatile int generation;
    private WifiManager.MulticastLock multicast;
    private boolean running, remoteReady, remoteActive, receiving, textSupported;
    private String status="未连接 · 点击开始发现", role="", peerName="", side="right", receivedClipboard="";
    private List<Peer> peers=Collections.emptyList();
    private PendingRequest pending;
    private int width=1080,height=1920;
    private float pointerX,pointerY;
    private long lastProblem;

    public static LinkService current(){return active;}
    @Override public void onCreate(){super.onCreate();active=this;createChannels();updateSize();}
    @Override public IBinder onBind(Intent intent){return binder;}
    @Override public int onStartCommand(Intent intent,int flags,int startId){
        if(intent!=null&&ACTION_STOP.equals(intent.getAction())){stopNetwork();stopSelf();return START_NOT_STICKY;}
        if(intent!=null&&ACTION_START.equals(intent.getAction()))startNetwork();
        return START_NOT_STICKY;
    }
    public void addObserver(Observer observer){observers.add(observer);observer.onStateChanged();}
    public void removeObserver(Observer observer){observers.remove(observer);}
    public boolean isRunning(){return running;}
    public boolean isDiscoveryReady(){KMLinkNode n=node;return running&&n!=null&&n.invitationPort()>0;}
    public int invitationPort(){KMLinkNode n=node;return n==null?0:n.invitationPort();}
    public boolean isConnected(){return node!=null&&node.isConnected();}
    public boolean isController(){return "controller".equals(role)&&remoteReady;}
    public boolean isControlling(){return isController()&&remoteActive;}
    public boolean isReceiving(){return receiving;}
    public String status(){return status;}
    public String role(){return role;}
    public String peerName(){return peerName;}
    public String clipboardText(){return receivedClipboard;}
    public List<Peer> peers(){return new ArrayList<>(peers);}
    public PendingRequest pending(){return pending;}
    public boolean canReceive(){RemoteAccessibilityService s=RemoteAccessibilityService.current();return s!=null&&s.isUnlocked();}
    private void changed(){for(Observer observer:observers)observer.onStateChanged();}
    private void state(String value){status=value;updateNotification();changed();}
    private void post(int token,Runnable action){main.post(()->{if(token==generation)action.run();});}

    private void startNetwork(){
        if(running)return;
        running=true;final int token=++generation;
        try {
            Notification notification=notification("正在发现局域网设备");
            if(Build.VERSION.SDK_INT>=29)startForeground(NOTIFICATION_ID,notification,ServiceInfo.FOREGROUND_SERVICE_TYPE_CONNECTED_DEVICE);
            else startForeground(NOTIFICATION_ID,notification);
        }catch(RuntimeException error){running=false;state("无法启动连接服务，请保持应用在前台重试。");return;}
        try{
            WifiManager wifi=(WifiManager)getApplicationContext().getSystemService(Context.WIFI_SERVICE);
            if(wifi!=null){multicast=wifi.createMulticastLock("KMLink discovery");multicast.setReferenceCounted(false);multicast.acquire();}
        }catch(RuntimeException ignored){multicast=null;}
        state("正在准备安全连接…");
        worker.execute(()->{
            try{
                AndroidTlsIdentity identity=new AndroidTlsIdentity();
                String id=getSharedPreferences("identity",MODE_PRIVATE).getString("device_id",null);
                if(id==null){id=UUID.randomUUID().toString();getSharedPreferences("identity",MODE_PRIVATE).edit().putString("device_id",id).apply();}
                String name=(Build.MANUFACTURER+" "+Build.MODEL).trim();if(name.length()>80)name=name.substring(0,80);
                KMLinkNode created=new KMLinkNode(identity,id,name,"Android "+Build.VERSION.RELEASE,new KMLinkNode.Listener(){
                    @Override public void onPeersChanged(List<Peer> list){post(token,()->{peers=new ArrayList<>(list);changed();});}
                    @Override public void onPairing(PendingRequest request){post(token,()->{
                        pending=request;state(request.incoming?"收到连接申请，请核对配对数字":"等待对方核对数字并同意");
                        if(request.incoming)notifyRequest(request);
                    });}
                    @Override public void onPairingEnded(String id,String reason){post(token,()->{
                        if(pending!=null&&pending.id.equals(id)){pending=null;manager().cancel(REQUEST_ID);}
                        if(!isConnected())state(reason);else changed();
                    });}
                    @Override public void onConnected(String localRole,String name){
                        if(token!=generation)return;final int link=linkGeneration.incrementAndGet();post(token,()->{if(link==linkGeneration.get())connected(localRole,name);});
                    }
                    @Override public void onMessage(JSONObject message){
                        if(token!=generation)return;
                        final int link=linkGeneration.get();
                        if(!incoming.offer(new Packet(token,link,message))){post(token,()->{if(link==linkGeneration.get()){incoming.clear();disconnect();state("输入消息过多，已安全断开。");}});return;}
                        if(drainScheduled.compareAndSet(false,true))main.post(drainInput);
                    }
                    @Override public void onDisconnected(String reason){
                        if(token!=generation)return;final int link=linkGeneration.incrementAndGet();post(token,()->{if(link==linkGeneration.get())disconnected(reason);});
                    }
                    @Override public void onError(String reason){post(token,()->state(reason));}
                });
                main.post(()->{
                    if(token!=generation||!running){created.stop();return;}
                    // Main-thread assignment/start is atomic relative to Stop.
                    node=created;
                    try{created.start();state("正在发现局域网设备 · 两端需连接同一网络");}
                    catch(RuntimeException failure){created.stop();if(node==created)node=null;stopNetwork();state("连接服务启动失败，请重试。");}
                });
            }catch(Exception error){post(token,()->{stopNetwork();state("连接服务启动失败，请检查系统网络后重试。");});}
        });
    }
    public void stopNetwork(){
        generation++;linkGeneration.incrementAndGet();running=false;incoming.clear();
        KMLinkNode previous=node;node=null;if(previous!=null)worker.execute(previous::stop);
        if(multicast!=null){try{if(multicast.isHeld())multicast.release();}catch(RuntimeException ignored){}multicast=null;}
        peers=Collections.emptyList();pending=null;clearControl();manager().cancel(REQUEST_ID);
        stopForeground(STOP_FOREGROUND_REMOVE);state("已停止连接服务");
    }
    public void refresh(){KMLinkNode n=node;if(n!=null)n.refresh();}
    public void request(Peer peer,String mode){
        if(node==null){state("连接服务尚未准备好");return;}
        if("be_controlled".equals(mode)&&!canReceive()){state("先启用 KMLink 辅助功能，并保持屏幕解锁。");return;}
        try{node.request(peer,mode);state("正在发送连接申请…");}catch(RuntimeException error){state("无法申请，设备可能已忙碌；请刷新后重试。");}
    }
    public void approve(String id){
        if(pending==null||!pending.id.equals(id)||node==null)return;
        if("receiver".equals(pending.localRole)&&!canReceive()){
            node.reject(id);state("未开启辅助功能或屏幕已锁定，已拒绝控制申请。");return;
        }
        node.approve(id);state("已同意，正在建立加密连接…");
    }
    public void reject(String id){if(node!=null)node.reject(id);}
    public void cancel(String id){if(node!=null)node.cancel(id);}
    public void disconnect(){linkGeneration.incrementAndGet();incoming.clear();if(node!=null)node.disconnect();clearControl();state("已断开控制连接，仍可发现设备");}
    private void connected(String localRole,String name){
        clearControl();receivedClipboard="";role=localRole;peerName=name;updateSize();
        if("receiver".equals(role)&&!canReceive()){disconnect();state("辅助功能不可用或屏幕锁定，已拒绝控制。");return;}
        JSONObject bounds=message(null,"x",0,"y",0,"width",width,"height",height);
        if(!send(message("hello","version",1,"role",role,"name",Build.MODEL,"platform","Android","bounds",bounds,"capabilities",new JSONArray().put("text_input"))))return;
        state("已建立加密连接，正在确认对端…");
    }
    private void disconnected(String reason){incoming.clear();clearControl();state(reason==null?"连接已断开":reason);}
    private void clearControl(){
        role="";peerName="";remoteReady=remoteActive=receiving=textSupported=false;
        RemoteAccessibilityService access=RemoteAccessibilityService.current();if(access!=null)access.resetInput();
    }
    private void updateSize(){Point size=displaySize(this);width=size.x;height=size.y;}
    public static Point displaySize(Context context){
        WindowManager manager=(WindowManager)context.getSystemService(WINDOW_SERVICE);
        DisplayMetrics metrics=new DisplayMetrics();manager.getDefaultDisplay().getRealMetrics(metrics);
        return new Point(Math.max(64,metrics.widthPixels),Math.max(64,metrics.heightPixels));
    }
    @Override public void onConfigurationChanged(Configuration configuration){
        super.onConfigurationChanged(configuration);Point size=displaySize(this);
        if((size.x!=width||size.y!=height)&&isConnected()){disconnect();state("屏幕方向已改变，请重新连接。");}
        width=size.x;height=size.y;
    }
    public void accessibilityChanged(){
        main.post(()->{if("receiver".equals(role)&&!canReceive()){disconnect();state("辅助功能已关闭，控制连接已断开。");}else changed();});
    }
    public void locked(){
        if(receiving){receiving=false;send(message("return","rx",.5,"ry",.5));state("手机已锁定，已收回控制。解锁后可重新切换。");}
    }
    public void reportInputProblem(String reason){
        long now=android.os.SystemClock.elapsedRealtime();if(now-lastProblem<1500)return;lastProblem=now;state(reason);
    }
    private void handleMessage(JSONObject value){
        try{
            String type=value.getString("type");
            if("hello".equals(type)){
                if(remoteReady)throw new IllegalArgumentException();integer(value,"version",1,1);
                String expected="controller".equals(role)?"receiver":"controller";
                if(!expected.equals(value.getString("role")))throw new IllegalArgumentException();
                JSONObject bounds=value.getJSONObject("bounds");
                integer(bounds,"x",-131072,131072);integer(bounds,"y",-131072,131072);
                integer(bounds,"width",64,65536);integer(bounds,"height",64,65536);
                JSONArray capabilities=value.optJSONArray("capabilities");
                if(capabilities!=null)for(int i=0;i<capabilities.length();i++)if("text_input".equals(capabilities.optString(i)))textSupported=true;
                remoteReady=true;state("controller".equals(role)?"已连接 "+peerName+" · 点击开始控制":"已连接 "+peerName+" · 等待电脑切换控制");return;
            }
            if(!remoteReady)throw new IllegalArgumentException();
            if("clipboard".equals(type)){
                String text=value.getString("text");if(text.getBytes(StandardCharsets.UTF_8).length>65536)throw new IllegalArgumentException();
                receivedClipboard=text;state("收到对端文本，可在应用中点击复制");return;
            }
            if("controller".equals(role)){
                if(!"return".equals(type))throw new IllegalArgumentException();
                number(value,"rx",0,1);number(value,"ry",0,1);remoteActive=false;send(message("leave"));
                state("对端已返回本机 · 可再次开始控制");return;
            }
            RemoteAccessibilityService access=RemoteAccessibilityService.current();
            if(access==null){disconnect();state("辅助功能已停止，连接已断开。");return;}
            if(!access.isUnlocked()){if("enter".equals(type))receiving=true;locked();return;}
            if("enter".equals(type)){
                String edge=value.getString("side");if(!isSide(edge))throw new IllegalArgumentException();
                side=edge;float rx=(float)number(value,"rx",0,1),ry=(float)number(value,"ry",0,1);
                pointerX=rx*(width-1);pointerY=ry*(height-1);
                switch(side){case "right":pointerX=16;break;case "left":pointerX=width-17;break;case "bottom":pointerY=16;break;case "top":pointerY=height-17;break;}
                access.resetInput();receiving=true;access.moveCursor(pointerX,pointerY);
                state("电脑正在控制手机 · 通知栏可随时停止");return;
            }
            if("leave".equals(type)){receiving=false;access.resetInput();state("已连接 · 等待下一次控制");return;}
            if(!receiving)return;
            switch(type){
                case "move":
                    float dx=(float)number(value,"dx",-4096,4096),dy=(float)number(value,"dy",-4096,4096);
                    pointerX=Math.max(0,Math.min(width-1,pointerX+dx));pointerY=Math.max(0,Math.min(height-1,pointerY+dy));
                    boolean crossed=("right".equals(side)&&dx<0&&pointerX<=0)||("left".equals(side)&&dx>0&&pointerX>=width-1)
                            ||("bottom".equals(side)&&dy<0&&pointerY<=0)||("top".equals(side)&&dy>0&&pointerY>=height-1);
                    if(crossed){receiving=false;access.resetInput();send(message("return","rx",pointerX/(width-1),"ry",pointerY/(height-1)));state("控制已返回电脑");}
                    else access.moveCursor(pointerX,pointerY);break;
                case "button":
                    String button=value.getString("button");if(!("left".equals(button)||"right".equals(button)||"middle".equals(button)))throw new IllegalArgumentException();
                    access.button(button,bool(value,"pressed"));break;
                case "scroll":access.scroll((float)number(value,"dx",-120,120),(float)number(value,"dy",-120,120),width,height);break;
                case "key":
                    String key=value.getString("key");boolean pressed=bool(value,"pressed");
                    if(key.startsWith("char:")){String character=key.substring(5);if(character.codePointCount(0,character.length())!=1||Character.isISOControl(character.codePointAt(0))||(character.codePointAt(0)>=0xD800&&character.codePointAt(0)<=0xDFFF))throw new IllegalArgumentException();}
                    else if(!key.matches("named:[a-z0-9_]{1,24}"))throw new IllegalArgumentException();
                    access.key(key,pressed);break;
                case "text":String text=value.getString("text");validateText(text);access.insertText(text);break;
                default:throw new IllegalArgumentException();
            }
        }catch(Exception malformed){disconnect();state("收到了无效的控制消息，已安全断开。");}
    }
    private static boolean isSide(String side){return "left".equals(side)||"right".equals(side)||"top".equals(side)||"bottom".equals(side);}
    public void setSide(String side){if(isSide(side)){if(remoteActive)stopControl();this.side=side;}}
    public void startControl(){
        if(!isController())return;
        if(send(message("enter","side",side,"rx",.5,"ry",.5))){remoteActive=true;state("正在控制 "+peerName+" · 在触控板上滑动");}
    }
    public void stopControl(){if(remoteActive)send(message("leave"));remoteActive=false;state("控制已暂停，连接仍保留");}
    public void move(float dx,float dy){if(isControlling())send(message("move","dx",Math.max(-4096,Math.min(4096,dx)),"dy",Math.max(-4096,Math.min(4096,dy))));}
    public void click(String button){if(isControlling()){send(message("button","button",button,"pressed",true));send(message("button","button",button,"pressed",false));}}
    public void mouseButton(String button,boolean pressed){if(isControlling())send(message("button","button",button,"pressed",pressed));}
    public void scroll(float dx,float dy){if(isControlling())send(message("scroll","dx",Math.max(-120,Math.min(120,dx)),"dy",Math.max(-120,Math.min(120,dy))));}
    public void key(String key,boolean pressed){if(isControlling())send(message("key","key",key,"pressed",pressed));}
    public void sendText(String text){
        if(!isControlling()){state("先点击开始控制，再发送文字。");return;}
        try{validateText(text);}catch(IllegalArgumentException e){state("文字需在 4096 字节内，仅允许普通文字、换行和制表符。");return;}
        if(textSupported){send(message("text","text",text));return;}
        if(text.codePointCount(0,text.length())>256){state("对端版本较旧，请升级后发送长文本。");return;}
        text.codePoints().forEach(cp->{String key=cp=='\n'?"named:enter":cp=='\t'?"named:tab":"char:"+new String(Character.toChars(cp));key(key,true);key(key,false);});
    }
    public void sendClipboard(String text){
        if(!isConnected()||!remoteReady)return;
        if(text.getBytes(StandardCharsets.UTF_8).length>65536){state("剪贴板文本超过 64 KiB，未发送。");return;}
        send(message("clipboard","text",text));state("已发送剪贴板文本；对端需开启文本同步。");
    }
    private boolean send(JSONObject value){
        KMLinkNode n=node;if(n==null||!n.send(value)){if(n!=null)n.disconnect();clearControl();state("连接已中断，请重新连接。");return false;}return true;
    }
    private static void validateText(String text){
        if(text.getBytes(StandardCharsets.UTF_8).length>4096)throw new IllegalArgumentException();
        if(text.codePoints().anyMatch(cp->(Character.isISOControl(cp)&&cp!='\n'&&cp!='\t')||(cp>=0xD800&&cp<=0xDFFF)))throw new IllegalArgumentException();
    }
    private static boolean bool(JSONObject value,String key)throws Exception{Object v=value.get(key);if(!(v instanceof Boolean))throw new IllegalArgumentException();return (Boolean)v;}
    private static double number(JSONObject value,String key,double minimum,double maximum)throws Exception{
        Object v=value.get(key);if(!(v instanceof Number))throw new IllegalArgumentException();double n=((Number)v).doubleValue();
        if(!Double.isFinite(n)||n<minimum||n>maximum)throw new IllegalArgumentException();return n;
    }
    private static int integer(JSONObject value,String key,int minimum,int maximum)throws Exception{double n=number(value,key,minimum,maximum);if(n!=Math.rint(n))throw new IllegalArgumentException();return(int)n;}
    private static JSONObject message(String type,Object...pairs){
        JSONObject value=new JSONObject();try{if(type!=null)value.put("type",type);for(int i=0;i<pairs.length;i+=2)value.put((String)pairs[i],pairs[i+1]);}catch(Exception error){throw new IllegalArgumentException(error);}return value;
    }
    private NotificationManager manager(){return(NotificationManager)getSystemService(NOTIFICATION_SERVICE);}
    private void createChannels(){
        manager().createNotificationChannel(new NotificationChannel(CHANNEL,"KMLink 连接",NotificationManager.IMPORTANCE_LOW));
        manager().createNotificationChannel(new NotificationChannel(REQUEST_CHANNEL,"KMLink 连接申请",NotificationManager.IMPORTANCE_HIGH));
    }
    private PendingIntent openIntent(){return PendingIntent.getActivity(this,1,new Intent(this,MainActivity.class).addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP),PendingIntent.FLAG_UPDATE_CURRENT|PendingIntent.FLAG_IMMUTABLE);}
    private Notification notification(String detail){
        PendingIntent stop=PendingIntent.getService(this,2,new Intent(this,LinkService.class).setAction(ACTION_STOP),PendingIntent.FLAG_UPDATE_CURRENT|PendingIntent.FLAG_IMMUTABLE);
        return new Notification.Builder(this,CHANNEL).setSmallIcon(R.drawable.ic_kmlink)
                .setContentTitle("KMLink · 局域网连接已开启").setContentText(detail).setContentIntent(openIntent())
                .setOngoing(true).setVisibility(Notification.VISIBILITY_PRIVATE)
                .addAction(new Notification.Action.Builder(null,"停止连接",stop).build()).build();
    }
    private void updateNotification(){if(running)try{manager().notify(NOTIFICATION_ID,notification(status));}catch(SecurityException ignored){}}
    private void notifyRequest(PendingRequest request){
        try{manager().notify(REQUEST_ID,new Notification.Builder(this,REQUEST_CHANNEL).setSmallIcon(R.drawable.ic_kmlink)
                .setContentTitle("KMLink · 收到连接申请").setContentText(request.peerName+"，打开应用核对配对数字")
                .setContentIntent(openIntent()).setAutoCancel(true).setVisibility(Notification.VISIBILITY_PRIVATE).build());}catch(SecurityException ignored){}
    }
    @Override public void onDestroy(){stopNetwork();worker.shutdown();if(active==this)active=null;super.onDestroy();}
}
