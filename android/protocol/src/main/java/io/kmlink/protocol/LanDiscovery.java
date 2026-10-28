package io.kmlink.protocol;

import java.io.*;
import java.net.*;
import java.util.*;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.function.BooleanSupplier;
import org.json.*;

final class LanDiscovery implements AutoCloseable, Runnable {
  static final int PORT = 24881;
  static final String GROUP = "239.255.77.77";

  interface Listener {
    void peers(List<Peer> peers);

    void error(String message);
  }

  private final String id, name, system;
  private final int port;
  private final BooleanSupplier busy;
  private final Listener listener;
  private final AtomicBoolean stopped = new AtomicBoolean(), refresh = new AtomicBoolean(true);
  private final Map<String, Entry> entries = new HashMap<>();
  private final Map<String, Long> rate = new HashMap<>();
  private volatile MulticastSocket socket;
  private Thread thread;
  private volatile List<Peer> snapshot = Collections.emptyList();
  private final List<NetworkInterface> interfaces = new ArrayList<>();

  private static final class Entry {
    final Peer peer;
    final long seen;

    Entry(Peer p, long n) {
      peer = p;
      seen = n;
    }
  }

  LanDiscovery(
      String id, String name, String system, int port, BooleanSupplier busy, Listener listener) {
    this.id = id;
    this.name = name;
    this.system = system;
    this.port = port;
    this.busy = busy;
    this.listener = listener;
  }

  void start() {
    thread = new Thread(this, "kmlink-discovery");
    thread.setDaemon(true);
    thread.start();
  }

  void refresh() {
    refresh.set(true);
  }

  List<Peer> peers() {
    return snapshot;
  }

  @Override
  public void close() {
    if (stopped.compareAndSet(false, true)) {
      broadcast(packet("bye"));
      Wire.close(socket);
      if (thread != null && thread != Thread.currentThread())
        try {
          thread.join(500);
        } catch (InterruptedException e) {
          Thread.currentThread().interrupt();
        }
    }
  }

  private byte[] packet(String kind) {
    JSONObject data =
        Wire.obj("app", "kmlink-discovery", "version", 1, "kind", kind, "device_id", id);
    if (kind.equals("announce")) {
      try {
        data.put("name", name);
        data.put("system", system);
        data.put("port", port);
        data.put("busy", busy.getAsBoolean());
      } catch (Exception e) {
        throw new IllegalArgumentException(e);
      }
    }
    return Wire.utf8(Wire.canonical(data));
  }

  private synchronized void broadcast(byte[] data) {
    MulticastSocket s = socket;
    if (s == null || s.isClosed()) return;
    for (NetworkInterface ni : interfaces) {
      try {
        s.setNetworkInterface(ni);
        s.send(new DatagramPacket(data, data.length, InetAddress.getByName(GROUP), PORT));
      } catch (IOException ignored) {
      }
    }
    try {
      s.send(new DatagramPacket(data, data.length, InetAddress.getByName("255.255.255.255"), PORT));
    } catch (IOException ignored) {
    }
  }

  @Override
  public void run() {
    try (MulticastSocket s = new MulticastSocket(null)) {
      socket = s;
      s.setReuseAddress(true);
      s.setBroadcast(true);
      s.setTimeToLive(1);
      s.setSoTimeout(200);
      s.bind(new InetSocketAddress(PORT));
      Enumeration<NetworkInterface> list = NetworkInterface.getNetworkInterfaces();
      while (list != null && list.hasMoreElements()) {
        NetworkInterface ni = list.nextElement();
        try {
          if (!ni.isUp() || ni.isLoopback()) continue;
          boolean ipv4 = false;
          Enumeration<InetAddress> addresses = ni.getInetAddresses();
          while (addresses.hasMoreElements())
            if (addresses.nextElement() instanceof Inet4Address) ipv4 = true;
          if (!ipv4) continue;
          s.joinGroup(new InetSocketAddress(GROUP, PORT), ni);
          interfaces.add(ni);
        } catch (IOException ignored) {
        }
      }
      if (interfaces.isEmpty())
        try {
          s.joinGroup(InetAddress.getByName(GROUP));
        } catch (IOException ignored) {
        }
      long next = 0, lastQuery = 0;
      byte[] buffer = new byte[65535];
      while (!stopped.get()) {
        long now = System.nanoTime();
        if (refresh.getAndSet(false)) {
          broadcast(packet("query"));
          next = 0;
        }
        if (now >= next) {
          broadcast(packet("announce"));
          next = now + 2_000_000_000L;
        }
        boolean changed = false;
        Iterator<Map.Entry<String, Entry>> it = entries.entrySet().iterator();
        while (it.hasNext())
          if (now - it.next().getValue().seen >= 7_000_000_000L) {
            it.remove();
            changed = true;
          }
        if (changed) publish();
        DatagramPacket packet = new DatagramPacket(buffer, buffer.length);
        try {
          s.receive(packet);
        } catch (SocketTimeoutException e) {
          continue;
        }
        if (packet.getLength() > 1400
            || !(packet.getAddress() instanceof Inet4Address)
            || packet.getAddress().isAnyLocalAddress()
            || packet.getAddress().isMulticastAddress()) continue;
        String ip = packet.getAddress().getHostAddress();
        Long last = rate.get(ip);
        if (last != null && now - last < 20_000_000L) continue;
        if (rate.size() > 256) rate.clear();
        rate.put(ip, now);
        try {
          JSONObject m = Wire.parse(Arrays.copyOf(packet.getData(), packet.getLength()));
          if (!"kmlink-discovery".equals(m.opt("app"))
              || !Wire.integer(m.opt("version"))
              || m.optLong("version") != 1) continue;
          String peerId = Wire.requireId(m.opt("device_id"));
          if (!peerId.matches("[A-Za-z0-9_-]{8,80}") || peerId.equals(id)) continue;
          String kind = String.valueOf(m.opt("kind"));
          if (kind.equals("query")) {
            if (m.length() != 4) continue;
            if (now - lastQuery >= 200_000_000L) {
              byte[] response = packet("announce");
              s.send(
                  new DatagramPacket(
                      response, response.length, packet.getAddress(), packet.getPort()));
              lastQuery = now;
            }
            continue;
          }
          if (kind.equals("bye")) {
            Entry old = entries.get(peerId);
            if (m.length() == 4 && old != null && old.peer.ip.equals(ip)) {
              entries.remove(peerId);
              publish();
            }
            continue;
          }
          if (!kind.equals("announce") || m.length() != 8 || !(m.opt("busy") instanceof Boolean))
            continue;
          Peer peer =
              new Peer(
                  peerId,
                  Wire.requireText(m.opt("name"), 80),
                  Wire.requireText(m.opt("system"), 80),
                  ip,
                  Wire.requirePort(m.opt("port")),
                  m.optBoolean("busy"));
          Entry old = entries.get(peerId);
          if (old == null && entries.size() >= 64) continue;
          // Prefer an observed RFC1918 LAN address over VPN/benchmark adapters.
          if (old != null
              && !old.peer.ip.equals(ip)
              && rank(old.peer.ip) < rank(ip)
              && now - old.seen < 7_000_000_000L) continue;
          entries.put(peerId, new Entry(peer, now));
          if (old == null || !same(old.peer, peer)) publish();
        } catch (IOException | RuntimeException ignored) {
        }
      }
    } catch (IOException e) {
      if (!stopped.get()) listener.error("局域网发现不可用，请检查 Wi-Fi 和网络权限。");
    } finally {
      socket = null;
      entries.clear();
      snapshot = Collections.emptyList();
    }
  }

  private static boolean same(Peer a, Peer b) {
    return a.name.equals(b.name)
        && a.system.equals(b.system)
        && a.ip.equals(b.ip)
        && a.port == b.port
        && a.busy == b.busy;
  }

  private static int rank(String ip) {
    try {
      byte[] b = InetAddress.getByName(ip).getAddress();
      int a = b[0] & 255, c = b[1] & 255;
      if (a == 10 || a == 192 && c == 168 || a == 172 && c >= 16 && c <= 31) return 0;
      if (a == 169 && c == 254) return 1;
      if (a == 127) return 4;
      return 3;
    } catch (Exception e) {
      return 5;
    }
  }

  private void publish() {
    ArrayList<Peer> peers = new ArrayList<>();
    for (Entry entry : entries.values()) peers.add(entry.peer);
    peers.sort(
        (a, b) -> {
          int c = a.name.compareToIgnoreCase(b.name);
          return c != 0 ? c : a.id.compareTo(b.id);
        });
    snapshot = Collections.unmodifiableList(peers);
    try {
      listener.peers(snapshot);
    } catch (RuntimeException ignored) {
    }
  }
}
