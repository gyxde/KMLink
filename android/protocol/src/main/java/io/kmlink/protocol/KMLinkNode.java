package io.kmlink.protocol;

import java.io.*;
import java.net.*;
import java.util.*;
import java.util.concurrent.*;
import java.util.concurrent.atomic.AtomicBoolean;
import javax.net.ssl.*;
import org.json.*;

/** LAN discovery, explicit consent and authenticated control transport. No input injection. */
public final class KMLinkNode {
  public interface Listener {
    default void onPeersChanged(List<Peer> peers) {}

    default void onPairing(PendingRequest request) {}

    default void onPairingEnded(String id, String reason) {}

    default void onConnected(String localRole, String peerName) {}

    default void onMessage(JSONObject message) {}

    default void onDisconnected(String reason) {}

    default void onError(String reason) {}
  }

  public static final String PROTOCOL = "kmlink-invite/1";
  private final TlsIdentity identity;
  private final String id, name, system;
  private final Listener listener;
  private volatile Run run;

  private static final class Run {
    final AtomicBoolean stop = new AtomicBoolean();
    final Object lock = new Object();
    final ExecutorService workers =
        Executors.newCachedThreadPool(
            r -> {
              Thread t = new Thread(r, "kmlink-network");
              t.setDaemon(true);
              return t;
            });
    final Set<Closeable> sockets =
        Collections.newSetFromMap(new ConcurrentHashMap<Closeable, Boolean>());
    final Semaphore slots = new Semaphore(4);
    volatile SSLServerSocket server;
    volatile int port;
    volatile Pending pending;
    volatile ControlChannel channel;
    volatile LanDiscovery discovery;
  }

  private static final class Pending {
    final String id, mode;
    final boolean outgoing;
    volatile Peer peer;
    volatile SSLSocket socket;
    volatile boolean cancelled, decided, approved;
    volatile String phase = "handshake";

    Pending(String id, String mode, boolean outgoing, Peer peer) {
      this.id = id;
      this.mode = mode;
      this.outgoing = outgoing;
      this.peer = peer;
    }

    String role() {
      return mode.equals("control") == outgoing ? "controller" : "receiver";
    }
  }

  public KMLinkNode(
      TlsIdentity identity, String deviceId, String name, String system, Listener listener) {
    if (identity == null || listener == null)
      throw new IllegalArgumentException("Missing identity/listener");
    try {
      this.id = Wire.requireId(deviceId);
      if (!id.matches("[A-Za-z0-9_-]{8,80}")) throw new IOException("Invalid discovery ID");
      this.name = Wire.requireText(name, 80);
      this.system = Wire.requireText(system, 80);
    } catch (IOException e) {
      throw new IllegalArgumentException(e.getMessage(), e);
    }
    this.identity = identity;
    this.listener = listener;
  }

  public synchronized void start() {
    start(true);
  }

  /** Disabling UDP is useful for headless interoperability tests. */
  public synchronized void start(boolean discover) {
    stop();
    Run r = new Run();
    run = r;
    r.workers.execute(() -> listen(r, discover));
  }

  public synchronized void stop() {
    Run r = run;
    run = null;
    if (r == null) return;
    r.stop.set(true);
    Pending p = r.pending;
    if (p != null) {
      p.cancelled = true;
      Wire.close(p.socket);
    }
    if (r.discovery != null) r.discovery.close();
    if (r.channel != null) r.channel.close();
    Wire.close(r.server);
    for (Closeable socket : r.sockets) Wire.close(socket);
    r.workers.shutdownNow();
  }

  public int invitationPort() {
    Run r = run;
    return r == null ? 0 : r.port;
  }

  public List<Peer> peers() {
    Run r = run;
    return r != null && r.discovery != null ? r.discovery.peers() : Collections.emptyList();
  }

  public void refresh() {
    Run r = run;
    if (r != null && r.discovery != null) r.discovery.refresh();
  }

  public boolean isConnected() {
    Run r = run;
    return r != null && r.channel != null && r.channel.connected();
  }

  public boolean isBusy() {
    Run r = run;
    return r != null && (r.pending != null || r.channel != null);
  }

  private boolean live(Run r) {
    return run == r && !r.stop.get();
  }

  private void emit(Run r, Runnable callback) {
    if (live(r))
      try {
        callback.run();
      } catch (RuntimeException ignored) {
        Pending p = r.pending;
        if (p != null) {
          p.cancelled = true;
          Wire.close(p.socket);
        }
        if (r.channel != null) r.channel.close();
      }
  }

  private void changed(Run r) {
    if (r.discovery != null) r.discovery.refresh();
  }

  private void track(Run r, Closeable socket) throws IOException {
    if (!live(r)) {
      Wire.close(socket);
      throw new InterruptedIOException("Stopped");
    }
    r.sockets.add(socket);
  }

  public String request(Peer peer, String mode) {
    if (peer == null || !("control".equals(mode) || "be_controlled".equals(mode)))
      throw new IllegalArgumentException("无效的连接请求");
    try {
      Wire.requireId(peer.id);
      Wire.requirePort(peer.port);
      Wire.requireText(peer.ip, 64);
      if (!peer.ip.matches("[0-9]+\\.[0-9]+\\.[0-9]+\\.[0-9]+")
          || !(InetAddress.getByName(peer.ip) instanceof Inet4Address)) throw new IOException();
    } catch (IOException e) {
      throw new IllegalArgumentException("无效的设备地址");
    }
    if (peer.id.equals(id)) throw new IllegalArgumentException("不能连接本机");
    Run r = run;
    if (r == null || r.port == 0 || !live(r)) throw new IllegalStateException("连接服务尚未就绪");
    Pending pending = new Pending(UUID.randomUUID().toString().replace("-", ""), mode, true, peer);
    synchronized (r.lock) {
      if (r.pending != null || r.channel != null || peer.busy)
        throw new IllegalStateException("设备正忙，请稍后重试");
      r.pending = pending;
    }
    changed(r);
    r.workers.execute(() -> client(r, pending));
    return pending.id;
  }

  public void approve(String requestId) {
    Run r = run;
    if (r == null) return;
    synchronized (r.lock) {
      Pending p = r.pending;
      if (p != null && p.id.equals(requestId) && !p.outgoing && p.phase.equals("approval")) {
        p.approved = true;
        p.decided = true;
      }
    }
  }

  public void reject(String requestId) {
    Run r = run;
    if (r == null) return;
    synchronized (r.lock) {
      Pending p = r.pending;
      if (p != null && p.id.equals(requestId) && !p.outgoing && p.phase.equals("approval")) {
        p.approved = false;
        p.decided = true;
      } else if (p != null && p.id.equals(requestId)) cancel(requestId);
    }
  }

  public void cancel(String requestId) {
    Run r = run;
    if (r == null) return;
    Pending p = r.pending;
    if (p == null || !p.id.equals(requestId)) return;
    p.cancelled = true;
    Wire.close(p.socket);
    if (p.phase.equals("preparing") && r.channel != null) r.channel.close();
  }

  public void disconnect() {
    Run r = run;
    if (r == null) return;
    Pending p = r.pending;
    if (p != null) cancel(p.id);
    ControlChannel channel = r.channel;
    if (channel != null) channel.close();
  }

  public boolean send(JSONObject message) {
    Run r = run;
    return r != null && r.channel != null && r.channel.send(message);
  }

  private JSONObject localIdentity() {
    return Wire.obj("id", id, "name", name, "system", system);
  }

  private Peer parseIdentity(Object value, String ip) throws IOException {
    if (!(value instanceof JSONObject)) throw new IOException("缺少设备信息");
    JSONObject o = (JSONObject) value;
    return new Peer(
        Wire.requireId(o.opt("id")),
        Wire.requireText(o.opt("name"), 80),
        Wire.requireText(o.opt("system"), 80),
        ip,
        0,
        false);
  }

  private void listen(Run r, boolean discover) {
    try {
      SSLServerSocket server =
          (SSLServerSocket) identity.serverContext().getServerSocketFactory().createServerSocket();
      r.server = server;
      track(r, server);
      server.setReuseAddress(true);
      server.bind(new InetSocketAddress("0.0.0.0", 0));
      server.setSoTimeout(200);
      r.port = server.getLocalPort();
      if (discover) {
        r.discovery =
            new LanDiscovery(
                id,
                name,
                system,
                r.port,
                () -> r.pending != null || r.channel != null,
                new LanDiscovery.Listener() {
                  public void peers(List<Peer> list) {
                    emit(r, () -> listener.onPeersChanged(list));
                  }

                  public void error(String message) {
                    emit(r, () -> listener.onError(message));
                  }
                });
        r.discovery.start();
      }
      while (live(r)) {
        SSLSocket socket;
        try {
          socket = (SSLSocket) server.accept();
        } catch (SocketTimeoutException e) {
          continue;
        }
        if (!r.slots.tryAcquire()) {
          Wire.close(socket);
          continue;
        }
        track(r, socket);
        r.workers.execute(() -> server(r, socket));
      }
    } catch (Exception e) {
      if (live(r)) emit(r, () -> listener.onError("无法启动连接服务，请检查网络权限。"));
    } finally {
      r.port = 0;
      Wire.close(r.server);
      if (r.server != null) r.sockets.remove(r.server);
    }
  }

  private void server(Run r, SSLSocket socket) {
    Pending p = null;
    String result = "";
    try {
      Wire.configure(socket);
      Wire.Reader reader =
          new Wire.Reader(socket.getInputStream(), () -> !live(r), Wire.MAX_INVITE);
      JSONObject request = reader.await(3000);
      if (!"request".equals(request.opt("type"))
          || !PROTOCOL.equals(request.opt("protocol"))
          || !id.equals(request.opt("target_id"))) throw new IOException("请求目标不匹配");
      String requestId = Wire.requireText(request.opt("request_id"), 32);
      if (!requestId.matches("[0-9a-f]{32}")) throw new IOException("无效请求标识");
      String mode = Wire.requireText(request.opt("mode"), 20);
      if (!mode.equals("control") && !mode.equals("be_controlled")) throw new IOException("无效控制方向");
      Peer peer = parseIdentity(request.opt("device"), socket.getInetAddress().getHostAddress());
      if (peer.id.equals(id)) throw new IOException("不能连接本机");
      String clientCommit = commitment(request.opt("nonce_commit"));
      synchronized (r.lock) {
        if (r.pending != null || r.channel != null) {
          Wire.write(
              socket.getOutputStream(),
              Wire.obj("type", "refused", "reason", "busy"),
              Wire.MAX_INVITE);
          return;
        }
        p = new Pending(requestId, mode, false, peer);
        p.socket = socket;
        r.pending = p;
      }
      changed(r);
      final Pending active = p;
      reader =
          new Wire.Reader(
              socket.getInputStream(), () -> !live(r) || active.cancelled, Wire.MAX_INVITE);
      byte[] serverNonce = Wire.random(32);
      JSONObject challenge =
          Wire.obj(
              "type",
              "challenge",
              "protocol",
              PROTOCOL,
              "request_id",
              requestId,
              "device",
              localIdentity(),
              "nonce_commit",
              Wire.commitment(serverNonce));
      Wire.write(socket.getOutputStream(), challenge, Wire.MAX_INVITE);
      JSONObject reveal = reader.await(3000);
      if (!"reveal".equals(reveal.opt("type"))) throw new IOException("Missing reveal");
      byte[] clientNonce = Wire.nonce(reveal.opt("nonce"));
      if (!Wire.equalHex(clientCommit, Wire.commitment(clientNonce)))
        throw new IOException("Commitment mismatch");
      Wire.write(
          socket.getOutputStream(),
          Wire.obj("type", "reveal", "nonce", Wire.base64(serverNonce)),
          Wire.MAX_INVITE);
      String code =
          Wire.comparisonCode(
              Wire.sha256(identity.certificateDer()), request, challenge, clientNonce, serverNonce);
      p.phase = "approval";
      PendingRequest prompt = new PendingRequest(p.id, code, p.role(), p.peer, p.mode, true);
      emit(r, () -> listener.onPairing(prompt));
      long deadline = System.nanoTime() + 45_000_000_000L;
      while (!p.decided) {
        check(r, p);
        if (System.nanoTime() > deadline) throw new SocketTimeoutException();
        JSONObject message = reader.poll();
        if (message != null) throw new IOException("Unexpected confirmation message");
      }
      check(r, p);
      if (!p.approved) {
        Wire.write(
            socket.getOutputStream(),
            Wire.obj("type", "refused", "reason", "declined"),
            Wire.MAX_INVITE);
        result = "已拒绝请求。";
        return;
      }
      String key = Wire.base64(Wire.random(32));
      Wire.write(
          socket.getOutputStream(),
          Wire.obj("type", "accepted", "request_id", p.id, "key", key),
          Wire.MAX_INVITE);
      coordinate(r, p, reader, key);
      result = "请求已获同意，正在连接。";
    } catch (Exception e) {
      if (p != null)
        result =
            p.cancelled
                ? "已取消请求。"
                : e instanceof SocketTimeoutException ? "连接请求已超时。" : "连接请求失败，请核对配对数字后重试。";
    } finally {
      Wire.close(socket);
      if (socket != null) r.sockets.remove(socket);
      finishPending(r, p, result);
      r.slots.release();
    }
  }

  private void client(Run r, Pending p) {
    SSLSocket socket = null;
    String result = "";
    try {
      check(r, p);
      socket = (SSLSocket) Wire.clientContext().getSocketFactory().createSocket();
      p.socket = socket;
      track(r, socket);
      socket.connect(new InetSocketAddress(p.peer.ip, p.peer.port), 3000);
      Wire.configure(socket);
      check(r, p);
      p.peer =
          new Peer(
              p.peer.id,
              p.peer.name,
              p.peer.system,
              socket.getInetAddress().getHostAddress(),
              p.peer.port,
              p.peer.busy);
      Wire.Reader reader =
          new Wire.Reader(socket.getInputStream(), () -> !live(r) || p.cancelled, Wire.MAX_INVITE);
      byte[] clientNonce = Wire.random(32);
      JSONObject request =
          Wire.obj(
              "type",
              "request",
              "protocol",
              PROTOCOL,
              "request_id",
              p.id,
              "target_id",
              p.peer.id,
              "device",
              localIdentity(),
              "mode",
              p.mode,
              "nonce_commit",
              Wire.commitment(clientNonce));
      Wire.write(socket.getOutputStream(), request, Wire.MAX_INVITE);
      JSONObject challenge = reader.await(3000);
      if ("refused".equals(challenge.opt("type"))) throw new BusyException();
      if (!"challenge".equals(challenge.opt("type"))
          || !PROTOCOL.equals(challenge.opt("protocol"))
          || !p.id.equals(challenge.opt("request_id"))) throw new IOException("Invalid challenge");
      Peer actual = parseIdentity(challenge.opt("device"), p.peer.ip);
      if (!actual.id.equals(p.peer.id)) throw new IOException("Wrong target");
      p.peer = actual;
      String serverCommit = commitment(challenge.opt("nonce_commit"));
      Wire.write(
          socket.getOutputStream(),
          Wire.obj("type", "reveal", "nonce", Wire.base64(clientNonce)),
          Wire.MAX_INVITE);
      JSONObject reveal = reader.await(3000);
      if (!"reveal".equals(reveal.opt("type"))) throw new IOException("Missing reveal");
      byte[] serverNonce = Wire.nonce(reveal.opt("nonce"));
      if (!Wire.equalHex(serverCommit, Wire.commitment(serverNonce)))
        throw new IOException("Commitment mismatch");
      byte[] cert = socket.getSession().getPeerCertificates()[0].getEncoded();
      String code =
          Wire.comparisonCode(Wire.sha256(cert), request, challenge, clientNonce, serverNonce);
      p.phase = "approval";
      PendingRequest prompt = new PendingRequest(p.id, code, p.role(), p.peer, p.mode, false);
      emit(r, () -> listener.onPairing(prompt));
      JSONObject accepted = reader.await(45000);
      if ("refused".equals(accepted.opt("type"))) {
        result = "对方拒绝了请求。";
        return;
      }
      if (!"accepted".equals(accepted.opt("type"))
          || !p.id.equals(accepted.opt("request_id"))
          || !(accepted.opt("key") instanceof String))
        throw new IOException("Invalid confirmation");
      String key = (String) accepted.opt("key");
      Wire.key(key);
      check(r, p);
      coordinate(r, p, reader, key);
      result = "请求已获同意，正在连接。";
    } catch (Exception e) {
      result =
          p.cancelled
              ? "已取消请求。"
              : e instanceof BusyException
                  ? "对方设备正忙。"
                  : e instanceof SocketTimeoutException ? "连接请求已超时。" : "无法连接设备，请刷新后重试。";
    } finally {
      Wire.close(socket);
      if (socket != null) r.sockets.remove(socket);
      finishPending(r, p, result);
    }
  }

  private static final class BusyException extends IOException {}

  private static String commitment(Object value) throws IOException {
    if (!(value instanceof String) || !((String) value).matches("[0-9a-f]{64}"))
      throw new IOException("Invalid commitment");
    return (String) value;
  }

  private void check(Run r, Pending p) throws IOException {
    if (!live(r) || p.cancelled) throw new InterruptedIOException("Cancelled");
  }

  private void coordinate(Run r, Pending p, Wire.Reader reader, String key) throws IOException {
    check(r, p);
    p.phase = "preparing";
    ControlChannel channel = new ControlChannel(owner(r), identity, p.peer, p.role(), key);
    synchronized (r.lock) {
      check(r, p);
      if (r.channel != null) throw new IOException("Busy");
      r.channel = channel;
    }
    changed(r);
    try {
      if (p.role().equals("receiver")) {
        int port = channel.listen();
        Wire.write(
            p.socket.getOutputStream(),
            Wire.obj("type", "ready", "request_id", p.id, "port", port),
            Wire.MAX_INVITE);
        JSONObject ack = reader.await(12000);
        if (!"handoff".equals(ack.opt("type")) || !p.id.equals(ack.opt("request_id")))
          throw new IOException("Invalid handoff");
      } else {
        JSONObject ready = reader.await(12000);
        if (!"ready".equals(ready.opt("type")) || !p.id.equals(ready.opt("request_id")))
          throw new IOException("Missing ready");
        int port = Wire.requirePort(ready.opt("port"));
        check(r, p);
        channel.connect(p.peer.ip, port);
        Wire.write(
            p.socket.getOutputStream(),
            Wire.obj("type", "handoff", "request_id", p.id),
            Wire.MAX_INVITE);
      }
      check(r, p);
      p.phase = "finished";
    } catch (IOException | RuntimeException e) {
      channel.close();
      synchronized (r.lock) {
        if (r.channel == channel) r.channel = null;
      }
      changed(r);
      throw e;
    }
  }

  private ControlChannel.Owner owner(Run r) {
    return new ControlChannel.Owner() {
      public boolean live() {
        return KMLinkNode.this.live(r);
      }

      public void connected(ControlChannel c, String role, Peer peer) {
        if (live() && r.channel == c) emit(r, () -> listener.onConnected(role, peer.name));
        else c.close();
      }

      public void message(ControlChannel c, JSONObject m) {
        if (live() && r.channel == c) emit(r, () -> listener.onMessage(m));
      }

      public void ended(ControlChannel c, String reason) {
        boolean notify = false;
        synchronized (r.lock) {
          if (r.channel == c) {
            r.channel = null;
            notify = true;
          }
        }
        changed(r);
        if (notify) emit(r, () -> listener.onDisconnected(reason));
      }

      public void execute(Runnable task) {
        if (live()) r.workers.execute(task);
      }

      public void track(Closeable s) {
        r.sockets.add(s);
        if (!live()) Wire.close(s);
      }

      public void untrack(Closeable s) {
        if (s != null) r.sockets.remove(s);
      }
    };
  }

  private void finishPending(Run r, Pending p, String result) {
    if (p == null) return;
    synchronized (r.lock) {
      if (r.pending == p) r.pending = null;
    }
    changed(r);
    if (!result.isEmpty()) emit(r, () -> listener.onPairingEnded(p.id, result));
  }
}
