package io.kmlink.protocol;

import static org.junit.Assert.*;

import java.util.concurrent.*;
import org.json.*;
import org.junit.*;

public class NodeTest {
  static TestIdentity identity;

  @BeforeClass
  public static void identity() throws Exception {
    identity = TestIdentity.temporary();
  }

  private static final class Events implements KMLinkNode.Listener {
    final BlockingQueue<PendingRequest> requests = new LinkedBlockingQueue<>();
    final BlockingQueue<String> connected = new LinkedBlockingQueue<>(),
        disconnected = new LinkedBlockingQueue<>();
    final BlockingQueue<JSONObject> messages = new LinkedBlockingQueue<>();
    KMLinkNode node;

    public void onPairing(PendingRequest r) {
      requests.add(r);
    }

    public void onConnected(String role, String name) {
      connected.add(role);
      node.send(
          Wire.obj(
              "type",
              "hello",
              "version",
              1,
              "role",
              role,
              "name",
              "test",
              "platform",
              "Android",
              "bounds",
              Wire.obj("x", 0, "y", 0, "width", 1080, "height", 1920)));
    }

    public void onDisconnected(String reason) {
      disconnected.add(reason);
    }

    public void onMessage(JSONObject message) {
      messages.add(message);
    }
  }

  private KMLinkNode node(String id, Events events) throws Exception {
    KMLinkNode node = new KMLinkNode(identity, id, id, "Android", events);
    events.node = node;
    node.start(false);
    long stop = System.nanoTime() + 3_000_000_000L;
    while (node.invitationPort() == 0 && System.nanoTime() < stop) Thread.sleep(10);
    assertTrue(node.invitationPort() > 0);
    return node;
  }

  private void connect(String mode) throws Exception {
    Events ea = new Events(), eb = new Events();
    KMLinkNode a = node("device-aaa", ea), b = node("device-bbb", eb);
    try {
      String id =
          a.request(
              new Peer("device-bbb", "B", "Android", "127.0.0.1", b.invitationPort(), false), mode);
      PendingRequest incoming = eb.requests.poll(3, TimeUnit.SECONDS),
          outgoing = ea.requests.poll(3, TimeUnit.SECONDS);
      assertNotNull(incoming);
      assertNotNull(outgoing);
      assertEquals(id, incoming.id);
      assertEquals(incoming.code, outgoing.code);
      assertNull(ea.connected.poll(100, TimeUnit.MILLISECONDS));
      b.approve(id);
      assertEquals(
          mode.equals("control") ? "controller" : "receiver",
          ea.connected.poll(4, TimeUnit.SECONDS));
      assertEquals(
          mode.equals("control") ? "receiver" : "controller",
          eb.connected.poll(4, TimeUnit.SECONDS));
      assertNotNull(ea.messages.poll(2, TimeUnit.SECONDS));
      assertNotNull(eb.messages.poll(2, TimeUnit.SECONDS));
      assertTrue(a.send(Wire.obj("type", "move", "dx", 2, "dy", 3)));
      JSONObject message = eb.messages.poll(2, TimeUnit.SECONDS);
      assertNotNull(message);
      assertEquals("move", message.optString("type"));
      a.disconnect();
      assertNotNull(eb.disconnected.poll(2, TimeUnit.SECONDS));
    } finally {
      a.stop();
      b.stop();
    }
  }

  @Test
  public void controlDirectionAuthenticatesAndTransports() throws Exception {
    connect("control");
  }

  @Test
  public void invitedControllerDirectionAuthenticatesAndTransports() throws Exception {
    connect("be_controlled");
  }

  @Test
  public void rejectionAndBusyNeverStartInput() throws Exception {
    Events ea = new Events(), eb = new Events();
    KMLinkNode a = node("device-aaa", ea), b = node("device-bbb", eb);
    try {
      String id =
          a.request(
              new Peer("device-bbb", "B", "Android", "127.0.0.1", b.invitationPort(), false),
              "control");
      assertNotNull(eb.requests.poll(3, TimeUnit.SECONDS));
      try {
        a.request(
            new Peer("device-bbb", "B", "Android", "127.0.0.1", b.invitationPort(), false),
            "control");
        fail();
      } catch (IllegalStateException expected) {
      }
      b.reject(id);
      Thread.sleep(250);
      assertFalse(a.isConnected());
      assertFalse(b.isConnected());
      assertTrue(ea.connected.isEmpty());
      assertTrue(eb.connected.isEmpty());
    } finally {
      a.stop();
      b.stop();
    }
  }

  @Test
  public void cancellationNeverCreatesControlChannel() throws Exception {
    Events ea = new Events(), eb = new Events();
    KMLinkNode a = node("device-aaa", ea), b = node("device-bbb", eb);
    try {
      String id =
          a.request(
              new Peer("device-bbb", "B", "Android", "127.0.0.1", b.invitationPort(), false),
              "control");
      assertNotNull(eb.requests.poll(3, TimeUnit.SECONDS));
      a.cancel(id);
      Thread.sleep(250);
      b.approve(id);
      assertFalse(a.isConnected());
      assertFalse(b.isConnected());
    } finally {
      a.stop();
      b.stop();
    }
  }
}
