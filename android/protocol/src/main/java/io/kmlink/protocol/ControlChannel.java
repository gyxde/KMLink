package io.kmlink.protocol;

import java.io.*;
import java.net.*;
import java.util.*;
import java.util.concurrent.*;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicInteger;
import javax.net.ssl.*;
import org.json.*;

final class ControlChannel implements AutoCloseable {
  interface Owner {
    boolean live();

    void connected(ControlChannel c, String role, Peer peer);

    void message(ControlChannel c, JSONObject m);

    void ended(ControlChannel c, String reason);

    void execute(Runnable task);

    void track(Closeable socket);

    void untrack(Closeable socket);
  }

  private final Owner owner;
  private final TlsIdentity identity;
  private final Peer peer;
  private final String role;
  private final byte[] key;
  private final AtomicBoolean stopped = new AtomicBoolean();
  private volatile boolean connected;
  private volatile SSLSocket socket;
  private volatile SSLServerSocket server;
  private volatile long received;
  private final BlockingQueue<byte[]> outgoing = new ArrayBlockingQueue<>(1024);
  private final AtomicInteger queuedBytes = new AtomicInteger();
  private volatile String reason = "连接已断开。";
  private ScheduledExecutorService watchdog;

  ControlChannel(Owner owner, TlsIdentity identity, Peer peer, String role, String key)
      throws IOException {
    this.owner = owner;
    this.identity = identity;
    this.peer = peer;
    this.role = role;
    this.key = Wire.key(key);
  }

  boolean connected() {
    return connected && !stopped.get();
  }

  private boolean ended() {
    return stopped.get() || !owner.live();
  }

  int listen() throws IOException {
    SSLServerSocket listening =
        (SSLServerSocket) identity.serverContext().getServerSocketFactory().createServerSocket();
    server = listening;
    owner.track(listening);
    listening.setReuseAddress(true);
    listening.bind(new InetSocketAddress("0.0.0.0", 0));
    listening.setSoTimeout(200);
    owner.execute(this::accept);
    return listening.getLocalPort();
  }

  void connect(String host, int port) {
    owner.execute(
        () -> {
          try {
            if (ended()) return;
            SSLSocket s = (SSLSocket) Wire.clientContext().getSocketFactory().createSocket();
            socket = s;
            owner.track(s);
            s.connect(new InetSocketAddress(host, port), 3000);
            Wire.configure(s);
            clientAuth(s);
            startSession(s);
          } catch (Exception e) {
            reason = ended() ? "已停止共享。" : "无法建立键鼠连接，请重新配对。";
          } finally {
            finish();
          }
        });
  }

  private void accept() {
    try {
      long deadline = System.nanoTime() + 15_000_000_000L;
      while (!ended() && System.nanoTime() < deadline) {
        SSLSocket s;
        try {
          s = (SSLSocket) server.accept();
        } catch (SocketTimeoutException e) {
          continue;
        }
        socket = s;
        owner.track(s);
        try {
          Wire.configure(s);
          serverAuth(s);
          Wire.close(server);
          owner.untrack(server);
          server = null;
          startSession(s);
          return;
        } catch (Exception e) {
          Wire.close(s);
          owner.untrack(s);
          socket = null;
        }
      }
      reason = ended() ? "已停止共享。" : "对方未完成连接，请重新申请。";
    } catch (Exception e) {
      reason = ended() ? "已停止共享。" : "接收连接失败。";
    } finally {
      finish();
    }
  }

  private void serverAuth(SSLSocket s) throws IOException {
    Wire.Reader in = new Wire.Reader(s.getInputStream(), this::ended, Wire.MAX_FRAME);
    JSONObject hello = in.await(3000);
    version(hello);
    byte[] client = Wire.nonce(hello.opt("nonce")), serverNonce = Wire.random(32);
    byte[] t = Wire.transcript(Wire.sha256(identity.certificateDer()), client, serverNonce);
    Wire.write(
        s.getOutputStream(),
        Wire.obj(
            "version", 1, "nonce", Wire.base64(serverNonce), "proof", Wire.proof(key, "server", t)),
        Wire.MAX_FRAME);
    JSONObject finish = in.await(3000);
    if (!Wire.equalHex(finish.opt("proof"), Wire.proof(key, "client", t)))
      throw new IOException("Invalid pairing key");
    Wire.write(s.getOutputStream(), Wire.obj("version", 1, "ok", true), Wire.MAX_FRAME);
  }

  private void clientAuth(SSLSocket s) throws IOException {
    Wire.Reader in = new Wire.Reader(s.getInputStream(), this::ended, Wire.MAX_FRAME);
    byte[] client = Wire.random(32);
    Wire.write(
        s.getOutputStream(), Wire.obj("version", 1, "nonce", Wire.base64(client)), Wire.MAX_FRAME);
    JSONObject response = in.await(3000);
    version(response);
    byte[] remote = Wire.nonce(response.opt("nonce"));
    byte[] cert;
    try {
      cert = s.getSession().getPeerCertificates()[0].getEncoded();
    } catch (Exception e) {
      throw new IOException(e);
    }
    byte[] t = Wire.transcript(Wire.sha256(cert), client, remote);
    if (!Wire.equalHex(response.opt("proof"), Wire.proof(key, "server", t)))
      throw new IOException("Pairing proof failed");
    Wire.write(
        s.getOutputStream(), Wire.obj("proof", Wire.proof(key, "client", t)), Wire.MAX_FRAME);
    JSONObject ack = in.await(3000);
    version(ack);
    if (ack.length() != 2 || !Boolean.TRUE.equals(ack.opt("ok")))
      throw new IOException("Pairing not acknowledged");
  }

  private static void version(JSONObject object) throws IOException {
    if (!Wire.integer(object.opt("version")) || object.optInt("version") != 1)
      throw new IOException("Unsupported version");
  }

  boolean send(JSONObject message) {
    if (!connected() || message == null || message.has("_kmlink_transport")) return false;
    try {
      byte[] data = Wire.utf8(Wire.canonical(message));
      if (data.length < 1 || data.length > Wire.MAX_FRAME) return false;
      byte[] frame = new byte[data.length + 4];
      frame[0] = (byte) (data.length >>> 24);
      frame[1] = (byte) (data.length >>> 16);
      frame[2] = (byte) (data.length >>> 8);
      frame[3] = (byte) data.length;
      System.arraycopy(data, 0, frame, 4, data.length);
      if (queuedBytes.addAndGet(frame.length) > 4 * 1024 * 1024) {
        queuedBytes.addAndGet(-frame.length);
        reason = "发送缓冲已满，已安全断开。";
        close();
        return false;
      }
      if (outgoing.offer(frame)) return true;
      queuedBytes.addAndGet(-frame.length);
      reason = "发送队列已满，已安全断开。";
      close();
      return false;
    } catch (RuntimeException e) {
      return false;
    }
  }

  private void startSession(SSLSocket s) throws IOException {
    if (ended()) return;
    connected = true;
    received = System.nanoTime();
    watchdog =
        Executors.newSingleThreadScheduledExecutor(
            r -> {
              Thread t = new Thread(r, "kmlink-link-watchdog");
              t.setDaemon(true);
              return t;
            });
    watchdog.scheduleAtFixedRate(
        () -> {
          if (connected() && System.nanoTime() - received > 6_000_000_000L) {
            reason = "对方无响应，已断开连接。";
            close();
          }
        },
        1,
        1,
        TimeUnit.SECONDS);
    owner.connected(this, role, peer);
    Wire.Reader reader = new Wire.Reader(s.getInputStream(), this::ended, Wire.MAX_FRAME);
    long heartbeat = 0;
    OutputStream out = s.getOutputStream();
    while (!ended()) {
      for (int i = 0; i < 64; i++) {
        byte[] frame = outgoing.poll();
        if (frame == null) break;
        queuedBytes.addAndGet(-frame.length);
        out.write(frame);
      }
      out.flush();
      long now = System.nanoTime();
      if (now - heartbeat >= 1_000_000_000L) {
        Wire.write(out, Wire.obj("_kmlink_transport", "heartbeat"), Wire.MAX_FRAME);
        heartbeat = now;
      }
      JSONObject message = reader.poll();
      if (message == null) continue;
      received = System.nanoTime();
      if (message.has("_kmlink_transport")) {
        if (message.length() != 1 || !"heartbeat".equals(message.opt("_kmlink_transport")))
          throw new IOException("Invalid heartbeat");
        continue;
      }
      owner.message(this, message);
    }
  }

  private void finish() {
    connected = false;
    stopped.set(true);
    if (watchdog != null) watchdog.shutdownNow();
    SSLSocket s = socket;
    SSLServerSocket ss = server;
    Wire.close(s);
    Wire.close(ss);
    owner.untrack(s);
    owner.untrack(ss);
    outgoing.clear();
    Arrays.fill(key, (byte) 0);
    owner.ended(this, reason);
  }

  @Override
  public void close() {
    stopped.set(true);
    connected = false;
    Wire.close(socket);
    Wire.close(server);
    if (watchdog != null) watchdog.shutdownNow();
  }
}
