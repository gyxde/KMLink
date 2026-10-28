package io.kmlink.protocol;

public final class PendingRequest {
  public final String id, code, localRole, peerName, peerSystem, peerIp, mode;
  public final boolean incoming;

  PendingRequest(
      String id, String code, String localRole, Peer peer, String mode, boolean incoming) {
    this.id = id;
    this.code = code;
    this.localRole = localRole;
    this.peerName = peer.name;
    this.peerSystem = peer.system;
    this.peerIp = peer.ip;
    this.mode = mode;
    this.incoming = incoming;
  }
}
