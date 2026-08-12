# BZRNet Common UDP Transport Header

Date: 2026-08-12

## High-confidence boundary

Independent evidence now converges on the same layout for Redux peer UDP:

- bytes `0..17`: common BZRNet transport header
- bytes `16..17`: unsigned 16-bit **big-endian** sequence number
- byte `18` onward: control / identity / object / state payload

The March 21 captures had already shown `SS`, `KA`, `PP`, `PR`, `7a75`, and
`0x5f` payloads beginning at offset 18. The separate
`PiercingXX/battlezone-netcode-patch` work reached the same boundary by
instrumenting the live game's Winsock path (`WSASendTo` and overlapped
`WSARecvFrom`/IOCP) and measuring the sequence field.

`server.decode_bzr_udp_datagram()` now reports this as `transportHeader` and
keeps bytes `2..15` opaque rather than inventing names. It also exposes the raw
first byte as `flagsByte`, byte 1 as `packetKind`, the 18-byte header hex, and a
reliability hint based on the observed `0x40`/`0xC0` split.

## Relay consequence

A matchmaking relay does not need to decode the payload after byte 18. The UDP
1339 fallback therefore preserves each complete datagram byte-for-byte and
leaves sequence/reliability behavior to the clients. The relay hot path samples
only the common 18-byte wrapper for diagnostics and does not run the deep
`7a75`/`0x5f` decoders on every gameplay packet. This avoids duplicating or
interfering with Redux's own transport machinery.

## Remaining uncertainty

This does **not** recover the official 1339 route-allocation/association
envelope. The community relay currently associates a same-lobby peer pair from
`DoP2PRoute` and their known WAN endpoints. If a raw source endpoint maps to
multiple target peers, forwarding is rejected as ambiguous rather than guessed.
That limitation should remain until an official relay capture provides a routing
or association identifier.
