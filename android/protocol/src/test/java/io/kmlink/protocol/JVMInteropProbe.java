package io.kmlink.protocol;

import java.io.*;
import java.nio.charset.StandardCharsets;
import java.nio.file.Paths;
import org.json.*;

/** Line-oriented harness used by Python CI to test real Java/Python interoperability. */
public final class JVMInteropProbe {
  private static synchronized void emit(JSONObject data) {
    System.out.println(Wire.canonical(data));
    System.out.flush();
  }

  public static void main(String[] args) throws Exception {
    System.setOut(new PrintStream(new FileOutputStream(FileDescriptor.out), true, "UTF-8"));
    if (args.length != 3)
      throw new IllegalArgumentException("PKCS12 path, test password, node id required");
    TestIdentity identity = new TestIdentity(Paths.get(args[0]), args[1]);
    final KMLinkNode[] holder = new KMLinkNode[1];
    KMLinkNode node =
        new KMLinkNode(
            identity,
            args[2],
            "Java 设备 🚀",
            "Android",
            new KMLinkNode.Listener() {
              public void onPairing(PendingRequest r) {
                emit(
                    Wire.obj(
                        "event",
                        "pairing",
                        "id",
                        r.id,
                        "code",
                        r.code,
                        "incoming",
                        r.incoming,
                        "role",
                        r.localRole,
                        "mode",
                        r.mode));
              }

              public void onPairingEnded(String id, String reason) {
                emit(Wire.obj("event", "pairing_ended", "id", id, "reason", reason));
              }

              public void onConnected(String role, String name) {
                emit(Wire.obj("event", "connected", "role", role));
                holder[0].send(
                    Wire.obj(
                        "type",
                        "hello",
                        "version",
                        1,
                        "role",
                        role,
                        "name",
                        "Java 设备 🚀",
                        "platform",
                        "Android",
                        "bounds",
                        Wire.obj("x", 0, "y", 0, "width", 1080, "height", 1920)));
              }

              public void onMessage(JSONObject m) {
                emit(Wire.obj("event", "message", "message", m));
              }

              public void onDisconnected(String reason) {
                emit(Wire.obj("event", "disconnected", "reason", reason));
              }

              public void onError(String reason) {
                emit(Wire.obj("event", "error", "reason", reason));
              }
            });
    holder[0] = node;
    node.start(false);
    long until = System.nanoTime() + 4_000_000_000L;
    while (node.invitationPort() == 0 && System.nanoTime() < until) Thread.sleep(5);
    if (node.invitationPort() == 0) throw new IOException("No listener");
    emit(Wire.obj("event", "ready", "port", node.invitationPort(), "id", args[2]));
    try (BufferedReader reader =
        new BufferedReader(new InputStreamReader(System.in, StandardCharsets.UTF_8))) {
      String line;
      while ((line = reader.readLine()) != null) {
        JSONObject c = Wire.parse(Wire.utf8(line));
        String cmd = c.optString("cmd");
        switch (cmd) {
          case "request":
            {
              String id =
                  node.request(
                      new Peer(
                          c.optString("id"),
                          "Python peer",
                          "Python",
                          c.optString("host", "127.0.0.1"),
                          c.optInt("port"),
                          false),
                      c.optString("mode", "control"));
              emit(Wire.obj("event", "requested", "id", id));
              break;
            }
          case "approve":
            node.approve(c.optString("id"));
            break;
          case "reject":
            node.reject(c.optString("id"));
            break;
          case "cancel":
            node.cancel(c.optString("id"));
            break;
          case "send":
            emit(Wire.obj("event", "sent", "ok", node.send(c.optJSONObject("message"))));
            break;
          case "disconnect":
            node.disconnect();
            break;
          case "stop":
            return;
          default:
            emit(Wire.obj("event", "error", "reason", "Unknown probe command"));
        }
      }
    } finally {
      node.stop();
    }
  }
}
