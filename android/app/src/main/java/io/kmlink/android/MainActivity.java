package io.kmlink.android;

import android.Manifest;
import android.app.Activity;
import android.app.AlertDialog;
import android.content.*;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.os.*;
import android.provider.Settings;
import android.view.*;
import android.widget.*;
import io.kmlink.protocol.Peer;
import io.kmlink.protocol.PendingRequest;
import java.util.List;
import java.util.HashMap;
import java.util.Map;

public final class MainActivity extends Activity implements LinkService.Observer {
    private static final int NOTIFICATIONS=401;
    private LinkService service;
    private boolean bound,foreground,suppressClose,pendingStart;
    private TextView status,permissionStatus,clipboardPreview;
    private Button start,stop,refresh,control,invite,toggleControl,sendText,copyIncoming,sendClipboard;
    private LinearLayout devices;
    private EditText text;
    private TouchpadView touchpad;
    private Spinner side;
    private String selectedId,dialogId;
    private AlertDialog dialog;
    private UpdateController updater;
    private TextView updateStatus;
    private Button updateCheck,updateInstall,updateCancel;
    private boolean updateBusy,updateAvailable;
    private final Map<Integer,String> forwardedKeys=new HashMap<>();
    private final ServiceConnection connection=new ServiceConnection(){
        public void onServiceConnected(ComponentName name,IBinder binder){service=((LinkService.LocalBinder)binder).service();bound=true;service.addObserver(MainActivity.this);}
        public void onServiceDisconnected(ComponentName name){service=null;render();}
    };
    @Override public void onCreate(Bundle state){
        super.onCreate(state);buildUi();
        updater=new UpdateController(this,(message,action,busy,available)->{
            updateStatus.setText(message);updateBusy=busy;updateAvailable=available;
            updateInstall.setText(action);renderUpdateButtons();
        },()->{if(service!=null)service.stopNetwork();},this::updatesAllowed);
    }
    @Override protected void onStart(){super.onStart();bound=bindService(new Intent(this,LinkService.class),connection,Context.BIND_AUTO_CREATE);}
    @Override protected void onResume(){super.onResume();foreground=true;if(updater!=null)updater.resume();render();}
    @Override protected void onPause(){foreground=false;if(updater!=null)updater.pause();releaseForwardedKeys();if(service!=null&&service.isControlling())service.stopControl();dismissDialog();super.onPause();}
    @Override protected void onDestroy(){if(updater!=null)updater.close();super.onDestroy();}
    @Override protected void onStop(){if(service!=null)service.removeObserver(this);if(bound){unbindService(connection);bound=false;}service=null;super.onStop();}
    @Override public void onStateChanged(){render();}
    private int dp(float value){return Math.round(value*getResources().getDisplayMetrics().density);}
    private TextView label(String value,int size,boolean bold){
        TextView view=new TextView(this);view.setText(value);view.setTextColor(Color.rgb(30,46,60));view.setTextSize(size);
        if(bold)view.setTypeface(Typeface.DEFAULT,Typeface.BOLD);
        LinearLayout.LayoutParams params=new LinearLayout.LayoutParams(-1,-2);params.bottomMargin=dp(10);view.setLayoutParams(params);return view;
    }
    private GradientDrawable rounded(int color){GradientDrawable d=new GradientDrawable();d.setColor(color);d.setCornerRadius(dp(14));return d;}
    private LinearLayout card(LinearLayout root,String title){
        LinearLayout box=new LinearLayout(this);box.setOrientation(LinearLayout.VERTICAL);box.setPadding(dp(18),dp(18),dp(18),dp(18));box.setBackground(rounded(Color.WHITE));
        LinearLayout.LayoutParams p=new LinearLayout.LayoutParams(-1,-2);p.bottomMargin=dp(16);root.addView(box,p);box.addView(label(title,18,true));return box;
    }
    private Button button(LinearLayout parent,String title,boolean primary,View.OnClickListener action){
        Button result=new Button(this);result.setText(title);result.setAllCaps(false);result.setTextColor(primary?Color.WHITE:Color.rgb(22,102,93));
        result.setBackgroundTintList(new android.content.res.ColorStateList(new int[][]{new int[]{android.R.attr.state_enabled},new int[]{}},new int[]{primary?Color.rgb(22,124,113):Color.rgb(231,244,240),Color.rgb(225,231,234)}));
        LinearLayout.LayoutParams p=new LinearLayout.LayoutParams(-1,dp(48));p.topMargin=dp(6);parent.addView(result,p);result.setOnClickListener(action);return result;
    }
    private void buildUi(){
        getWindow().setSoftInputMode(WindowManager.LayoutParams.SOFT_INPUT_ADJUST_RESIZE);
        ScrollView scroll=new ScrollView(this);scroll.setFillViewport(true);scroll.setBackgroundColor(Color.rgb(244,246,248));
        LinearLayout root=new LinearLayout(this);root.setOrientation(LinearLayout.VERTICAL);root.setPadding(dp(18),dp(20),dp(18),dp(24));scroll.addView(root);
        scroll.setOnApplyWindowInsetsListener((v,i)->{v.setPadding(0,i.getSystemWindowInsetTop(),0,i.getSystemWindowInsetBottom());return i;});setContentView(scroll);
        root.addView(label("KMLink   Android "+UpdateController.installedVersion(this),14,true));root.addView(label("连接电脑，也让电脑控制手机。",25,true));root.addView(label("同一局域网 · 配对数字确认 · 加密直连",13,false));
        status=label("未连接 · 点击开始发现",15,true);root.addView(status);
        LinearLayout network=card(root,"1  发现与连接");
        start=button(network,"开始发现设备",true,v->requestStart());
        stop=button(network,"停止所有连接",false,v->{if(service!=null)service.stopNetwork();});
        refresh=button(network,"刷新设备列表",false,v->{if(service!=null)service.refresh();});
        devices=new LinearLayout(this);devices.setOrientation(LinearLayout.VERTICAL);network.addView(devices);
        control=button(network,"申请控制选中设备",true,v->request("control"));invite=button(network,"邀请对方控制本机",false,v->request("be_controlled"));
        network.addView(label("核对两端六位数字，由收到请求的一方同意。未同意前不会开始控制。",12,false));
        LinearLayout access=card(root,"2  允许电脑控制手机");permissionStatus=label("辅助功能未开启",14,true);access.addView(permissionStatus);
        button(access,"打开辅助功能设置",false,v->startActivity(new Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS)));
        access.addView(label("手动启用 KMLink。控制期间显示光标，通知栏可随时停止。保持屏幕解锁；密码框、安全界面和锁屏不支持远程输入。",12,false));
        LinearLayout pad=card(root,"3  用手机控制电脑");pad.addView(label("触控板仅在此应用内工作；离开应用会暂停控制。",12,false));
        side=new Spinner(this);side.setAdapter(new ArrayAdapter<>(this,android.R.layout.simple_spinner_dropdown_item,new String[]{"电脑在右侧 →","电脑在左侧 ←","电脑在上方 ↑","电脑在下方 ↓"}));pad.addView(side);
        toggleControl=button(pad,"开始控制电脑",true,v->{if(service==null)return;if(service.isControlling())service.stopControl();else{service.setSide(new String[]{"right","left","top","bottom"}[side.getSelectedItemPosition()]);service.startControl();}});
        touchpad=new TouchpadView(this);touchpad.setOnFocusChangeListener((view,focused)->{if(!focused)releaseForwardedKeys();});pad.addView(touchpad,new LinearLayout.LayoutParams(-1,dp(205)));
        touchpad.setSink(new TouchpadView.Sink(){
            public void move(float dx,float dy){if(service!=null)service.move(dx,dy);}
            public void scroll(float dx,float dy){if(service!=null)service.scroll(dx,dy);}
            public void click(){if(service!=null)service.click("left");}
            public void drag(boolean down){if(service!=null)service.mouseButton("left",down);}
        });
        LinearLayout row=new LinearLayout(this);pad.addView(row);
        for(String name:new String[]{"左键","右键","退格","Enter"}){
            Button item=new Button(this);item.setText(name);row.addView(item,new LinearLayout.LayoutParams(0,dp(48),1));
            item.setOnClickListener(v->{if(service==null)return;switch(name){case "左键":service.click("left");break;case "右键":service.click("right");break;case "退格":service.key("named:backspace",true);service.key("named:backspace",false);break;default:service.key("named:enter",true);service.key("named:enter",false);}});
        }
        text=new EditText(this);text.setHint("输入文字，再发送到电脑当前输入框");text.setMinLines(2);text.setGravity(Gravity.TOP);pad.addView(text,new LinearLayout.LayoutParams(-1,-2));
        sendText=button(pad,"发送文字",false,v->{if(service!=null)service.sendText(text.getText().toString());});
        LinearLayout clipboard=card(root,"4  文本剪贴板");clipboard.addView(label("仅在点击按钮时读取本机剪贴板。对端需启用文本同步。",12,false));
        sendClipboard=button(clipboard,"发送本机剪贴板",false,v->sendLocalClipboard());clipboardPreview=label("尚未收到对端文本",13,false);clipboard.addView(clipboardPreview);
        copyIncoming=button(clipboard,"复制收到的文本",false,v->{if(service!=null){((ClipboardManager)getSystemService(CLIPBOARD_SERVICE)).setPrimaryClip(ClipData.newPlainText("KMLink",service.clipboardText()));toast("已复制");}});
        root.addView(label("没有屏幕投屏。Android 通过辅助功能点击、滑动和编辑可访问的输入框；拖动在鼠标松开后执行。部分应用和厂商系统可能限制操作。",12,false));
        LinearLayout updates=card(root,"5  软件更新");
        updateStatus=label("更新来源：GitHub gyxde/KMLink",13,false);updates.addView(updateStatus);
        updateCheck=button(updates,"检查新版本",false,v->{if(updater!=null&&updatesAllowed())updater.check(true);});
        updateInstall=button(updates,"下载并安装更新",true,v->{if(updater!=null)updater.downloadOrInstall();});updateInstall.setEnabled(false);
        updateCancel=button(updates,"取消下载",false,v->{if(updater!=null)updater.cancel();});updateCancel.setEnabled(false);
        updates.addView(label("启动时自动检查，最多每 6 小时一次。停止控制连接后，从本机发起更新。下载后验证版本、SHA-256 和应用签名，再由 Android 系统确认安装。",12,false));
    }
    private void requestStart(){
        if(Build.VERSION.SDK_INT>=33&&checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS)!=PackageManager.PERMISSION_GRANTED){pendingStart=true;requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS},NOTIFICATIONS);return;}
        startNetwork();
    }
    private void startNetwork(){startForegroundService(new Intent(this,LinkService.class).setAction(LinkService.ACTION_START));}
    @Override public void onRequestPermissionsResult(int request,String[] permissions,int[] results){
        super.onRequestPermissionsResult(request,permissions,results);
        if(request==NOTIFICATIONS&&pendingStart){pendingStart=false;startNetwork();if(results.length==0||results[0]!=PackageManager.PERMISSION_GRANTED)toast("通知未允许，请保持应用在前台接收配对请求。");}
    }
    private void request(String mode){
        Peer peer=selectedPeer();if(service==null||peer==null)return;
        if("be_controlled".equals(mode)&&!service.canReceive()){toast("请先启用辅助功能，并解锁屏幕。");return;}service.request(peer,mode);
    }
    private Peer selectedPeer(){if(service==null||selectedId==null)return null;for(Peer peer:service.peers())if(selectedId.equals(peer.id))return peer;return null;}
    private void render(){
        if(status==null)return;
        boolean running=service!=null&&service.isRunning(),connected=service!=null&&service.isConnected();
        status.setText(service==null?"连接服务未启动":service.status());start.setEnabled(!running);stop.setEnabled(running);refresh.setEnabled(running);
        permissionStatus.setText(RemoteAccessibilityService.available()?"辅助功能已开启":"辅助功能未开启 · 被控制前需要开启");
        devices.removeAllViews();List<Peer> peers=service==null?java.util.Collections.emptyList():service.peers();
        if(peers.isEmpty())devices.addView(label(running?"正在寻找其他 KMLink 设备…":"开始发现后显示局域网设备。",13,false));
        for(Peer peer:peers){TextView view=label(peer.name+"\n"+peer.system+" · "+peer.ip+" · "+(peer.busy?"使用中":"可连接"),14,false);view.setPadding(dp(12),dp(12),dp(12),dp(12));view.setBackground(rounded(peer.id.equals(selectedId)?Color.rgb(207,237,226):Color.rgb(244,247,248)));view.setOnClickListener(v->{selectedId=peer.id;render();});devices.addView(view);}
        Peer peer=selectedPeer();PendingRequest pending=service==null?null:service.pending();boolean choose=running&&!connected&&pending==null&&peer!=null&&!peer.busy;
        control.setEnabled(choose);invite.setEnabled(choose);
        boolean controller=service!=null&&service.isController();toggleControl.setEnabled(controller);toggleControl.setText(service!=null&&service.isControlling()?"暂停控制电脑":"开始控制电脑");
        touchpad.setActive(service!=null&&service.isControlling());sendText.setEnabled(controller);side.setEnabled(service==null||!service.isControlling());
        sendClipboard.setEnabled(connected);copyIncoming.setEnabled(connected&&service!=null&&!service.clipboardText().isEmpty());
        if(service!=null){String received=service.clipboardText();clipboardPreview.setText(received.isEmpty()?"尚未收到对端文本":received.length()>160?received.substring(0,160)+"…":received);}
        if(foreground&&pending!=null&&!pending.id.equals(dialogId))showPairing(pending);if(pending==null&&dialog!=null)dismissDialog();
        renderUpdateButtons();
    }
    private boolean updatesAllowed(){return service==null||(!service.isConnected()&&service.pending()==null);}
    private void renderUpdateButtons(){
        if(updateCheck==null)return;boolean local=updatesAllowed();
        updateCheck.setEnabled(local&&!updateBusy);updateInstall.setEnabled(local&&updateAvailable&&!updateBusy);updateCancel.setEnabled(local&&updateBusy);
    }
    private void showPairing(PendingRequest request){
        dismissDialog();dialogId=request.id;boolean receiver="receiver".equals(request.localRole);
        String heading=request.incoming?(receiver?"对方申请控制此手机":"对方邀请你控制它"):(receiver?"邀请对方控制此手机":"申请控制对方设备");
        LinearLayout content=new LinearLayout(this);content.setOrientation(LinearLayout.VERTICAL);content.setPadding(dp(24),dp(12),dp(24),dp(8));
        content.addView(label(request.peerName+"\n"+request.peerSystem+" · "+request.peerIp,14,false));content.addView(label("请核对两台设备显示的数字完全一致",14,true));
        TextView code=label(request.code,36,true);code.setGravity(Gravity.CENTER);code.setTextColor(Color.rgb(22,124,113));content.addView(code);
        content.addView(label(receiver?"同意后，对方可在屏幕解锁时点击、滑动和输入。":"同意后，可用本应用触控板和文字输入控制对方。",13,false));
        AlertDialog.Builder builder=new AlertDialog.Builder(this).setTitle(heading).setView(content).setNegativeButton(request.incoming?"拒绝":"取消请求",(d,w)->{if(service!=null)service.cancel(request.id);});
        if(request.incoming)builder.setPositiveButton("数字一致，同意",null);
        dialog=builder.create();dialog.setCanceledOnTouchOutside(false);dialog.setOnCancelListener(d->{if(!suppressClose&&service!=null)service.cancel(request.id);});
        dialog.setOnShowListener(d->{dialog.getButton(AlertDialog.BUTTON_NEGATIVE).requestFocus();if(request.incoming)dialog.getButton(AlertDialog.BUTTON_POSITIVE).setOnClickListener(v->{if(service==null)return;if(receiver&&!service.canReceive()){service.cancel(request.id);dismissDialog();toast("先开启辅助功能并保持解锁，再重新申请。");return;}dialog.getButton(AlertDialog.BUTTON_POSITIVE).setEnabled(false);service.approve(request.id);});});dialog.show();
    }
    private void dismissDialog(){suppressClose=true;if(dialog!=null)dialog.dismiss();dialog=null;dialogId=null;suppressClose=false;}
    private void sendLocalClipboard(){
        if(service==null)return;ClipData data=((ClipboardManager)getSystemService(CLIPBOARD_SERVICE)).getPrimaryClip();
        if(data==null||data.getItemCount()==0||data.getItemAt(0).getText()==null){toast("剪贴板没有纯文本");return;}service.sendClipboard(data.getItemAt(0).getText().toString());
    }
    private void toast(String value){Toast.makeText(this,value,Toast.LENGTH_LONG).show();}
    @Override public boolean dispatchKeyEvent(KeyEvent event){
        if(service!=null&&service.isControlling()&&touchpad.hasFocus()){
            if(event.getKeyCode()==KeyEvent.KEYCODE_ESCAPE&&event.isCtrlPressed()&&event.isAltPressed()){if(event.getAction()==KeyEvent.ACTION_DOWN){releaseForwardedKeys();service.stopControl();}return true;}
            if(event.getKeyCode()==KeyEvent.KEYCODE_F8){if(event.getAction()==KeyEvent.ACTION_UP){releaseForwardedKeys();service.stopControl();}return true;}
            String key=keyCode(event);if(key!=null&&(event.getAction()==KeyEvent.ACTION_DOWN||event.getAction()==KeyEvent.ACTION_UP)){
                if(event.getAction()==KeyEvent.ACTION_DOWN){forwardedKeys.putIfAbsent(event.getKeyCode(),key);service.key(forwardedKeys.get(event.getKeyCode()),true);}
                else{String original=forwardedKeys.remove(event.getKeyCode());if(original!=null)service.key(original,false);}return true;
            }
        }return super.dispatchKeyEvent(event);
    }
    private void releaseForwardedKeys(){if(service!=null)for(String key:forwardedKeys.values())service.key(key,false);forwardedKeys.clear();}
    @Override public void onWindowFocusChanged(boolean focused){super.onWindowFocusChanged(focused);if(!focused)releaseForwardedKeys();}
    private String keyCode(KeyEvent event){
        switch(event.getKeyCode()){
            case KeyEvent.KEYCODE_CTRL_LEFT:return "named:ctrl_l";case KeyEvent.KEYCODE_CTRL_RIGHT:return "named:ctrl_r";
            case KeyEvent.KEYCODE_SHIFT_LEFT:return "named:shift_l";case KeyEvent.KEYCODE_SHIFT_RIGHT:return "named:shift_r";
            case KeyEvent.KEYCODE_ALT_LEFT:return "named:alt_l";case KeyEvent.KEYCODE_ALT_RIGHT:return "named:alt_r";
            case KeyEvent.KEYCODE_DEL:return "named:backspace";case KeyEvent.KEYCODE_FORWARD_DEL:return "named:delete";
            case KeyEvent.KEYCODE_ENTER:return "named:enter";case KeyEvent.KEYCODE_TAB:return "named:tab";case KeyEvent.KEYCODE_ESCAPE:return "named:esc";
            case KeyEvent.KEYCODE_DPAD_LEFT:return "named:left";case KeyEvent.KEYCODE_DPAD_RIGHT:return "named:right";
            case KeyEvent.KEYCODE_DPAD_UP:return "named:up";case KeyEvent.KEYCODE_DPAD_DOWN:return "named:down";
            case KeyEvent.KEYCODE_MOVE_HOME:return "named:home";case KeyEvent.KEYCODE_MOVE_END:return "named:end";
            default:int cp=event.getUnicodeChar(0);return cp>0&&!Character.isISOControl(cp)?"char:"+new String(Character.toChars(cp)):null;
        }
    }
}
