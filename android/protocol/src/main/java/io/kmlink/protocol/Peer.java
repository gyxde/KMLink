package io.kmlink.protocol;

/** Unauthenticated LAN metadata; identity is established by comparing pairing digits. */
public final class Peer {
  public final String id, name, system, ip;
  public final int port;
  public final boolean busy;

  public Peer(String id, String name, String system, String ip, int port, boolean busy) {
    this.id = id;
    this.name = name;
    this.system = system;
    this.ip = ip;
    this.port = port;
    this.busy = busy;
  }
}
