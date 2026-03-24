# Official Run 1 Traffic Analysis

Date: 2026-03-21

## Artifacts

This note is based on the official two-PC run captured on March 21, 2026:

- Ian PC:
  - `game1.pcapng`
  - `BZLogger.txt`
- Kateri PC:
  - `Kat_PC.pcapng`
  - `BZLogger.txt`

Both clients were on the same LAN and shared the same public WAN address:

- `216.71.46.47`

Client game ports observed in auth and peer traffic:

- Ian: `192.168.0.44:65071`
- Kateri: `192.168.0.63:50745`

## High-Confidence Findings

### 1. UDP 1338 Reply Trailer Is Public Endpoint Data

The official bootstrap service at `68.183.35.188:1338` replies by echoing the
18-byte client probe and appending a 7-byte trailer:

- `0x04`
- 4 bytes of the observed public IPv4 address
- 2 bytes of the observed public UDP port, big-endian

Observed examples:

- Ian request:
  - `40020000019d119834f30000000000000000`
- Ian reply:
  - `40020000019d119834f3000000000000000004d8472e2ffe2f`
  - trailer `04 d8 47 2e 2f fe 2f` = `216.71.46.47:65071`

- Kateri request:
  - `40020000019d1198d3870000000000000000`
- Kateri reply:
  - `40020000019d1198d387000000000000000004d8472e2fc639`
  - trailer `04 d8 47 2e 2f c6 39` = `216.71.46.47:50745`

This replaces the earlier "server time" hypothesis.

### 2. UDP 1338 Probe Cadence

The bootstrap probe is not one-shot. It continues after auth, in lobby, and in
match:

- initial probe immediately after WebSocket auth
- second probe roughly 10 seconds later
- then roughly every 30 seconds

Ian examples from `game1.pcapng` relative time:

- `83.1828946`
- `93.1856869`
- `123.1907250`
- `153.2209376`
- `183.2256268`

Kateri shows the same behavior.

Implication:

- the official backend keeps receiving NAT-refresh style probes even after
  peer-to-peer traffic is active

### 3. WebSocket Session Ordering Is Clear

Observed startup ordering on both PCs:

1. TCP connect to `68.183.35.188:1337`
2. HTTP upgrade to WebSocket
3. client `Authorization`
4. server `OnAuthorization`
5. server `OnWhitelistUpdated`
6. client `DoEnterLounge`
7. server `OnLobbyListChanged`

Observed join flow for Kateri:

1. `DoJoinLobby {"id":1356,"password":"t"}`
2. `OnLobbyJoined`
3. repeated `OnLobbyDataChanged` / `OnUserDataChanged`
4. `OnLobbyMemberP2PConnect` for both peers

Observed host flow for Ian:

1. `CreateGame {"isPrivate":true,"memberLimit":8,"password":"t"}`
2. `OnLobbyCreated {"id":1356}`
3. repeated `SetLobbyData` / `SetPlayerData`
4. `DoP2PConnect`
5. `OnLobbyMemberP2PConnect`

### 4. The Official Service Explicitly Participates In P2P Handoff

This run includes both:

- `DoP2PConnect`
- `OnLobbyMemberP2PConnect`

That means peer handoff is not just inferred from lobby metadata. The official
service explicitly signals the transition.

Important nuance:

- first direct peer UDP can arrive before the host has received its own
  `OnLobbyMemberP2PConnect`
- the signaling and first peer datagrams race closely enough that the shim
  should not assume a strict ordering boundary

### 5. First Direct Peer UDP Starts In The Lobby

The first direct packets between the two clients appear immediately after the
join/P2P stage, before launch:

- Ian capture first Kateri -> Ian peer packet:
  - relative `143.4118203`
  - `192.168.0.63:50745 -> 192.168.0.44:65071`
- Ian receives `OnLobbyMemberP2PConnect` for Kateri at:
  - relative `143.5271684`

The first peer datagrams are short identity-style packets.

Observed 45-byte payloads:

- Ian -> Kateri:
  - `40030000019d1199210b000000000000000053373635363131393832343132353937303000`
  - ASCII tail decodes to `S76561198241259700\0`
- Kateri -> Ian:
  - `40030000019d11991dd3000000000000000053373635363131393832383230323338373900`
  - ASCII tail decodes to `S76561198282023879\0`

Related early packet families also appear as:

- `0x4004...`
- `0x4005...`

These look like repeated identity / hello variants rather than full gameplay
state.

### 6. Match-State Traffic Ramps Later

Heavier peer traffic begins after the launch sequence, not at first P2P contact.

Launch-related WS events:

- Kateri `SetPlayerData {"key":"launched","value":"1"}` at relative `135.3115006`
  in `Kat_PC.pcapng`
- Ian sees the corresponding `OnUserDataChanged` / `OnLobbyDataChanged` updates
  at relative `166.486...` to `166.622...` in `game1.pcapng`

A few seconds later, peer UDP shifts into much denser packet families:

- `0x5f...` packets carrying object/state blocks
- repeating small control packets with visible markers such as:
  - `SS`
  - `KA`
  - `PP`
  - `PR`

The larger gameplay packets frequently embed unit or object strings, including:

- `bvtump`
- `cvturr`
- `cvrremp`

This lines up with a model of:

- lobby-time direct connectivity first
- then state/object replication after launch

### 7. WebSocket Goes Mostly Quiet During Active Match

After launch, the WebSocket channel is not carrying gameplay. It mostly carries:

- metadata updates around ready/launch state
- lobby teardown updates at the end
- short periodic heartbeat frames

Observed heartbeat cadence on Kateri's WebSocket stream:

- client 6-byte frame at roughly `145.29`, `190.29`, `235.30`, `280.30`
- server 2-byte response roughly 0.11 to 0.12 seconds later

Implication:

- the official server remains present as a control plane
- actual match traffic is direct peer UDP

### 8. Current Packet-Family Heuristics Are Strong Enough For Tooling

The March 21 run supports a practical first-pass classifier for direct peer UDP:

- `bootstrap_probe`
  - `0x4002...` and 18 bytes long
- `bootstrap_reply`
  - `0x4002...` plus 7-byte endpoint trailer
- `peer_identity`
  - `0x4003`, `0x4004`, or `0x4005` variants carrying a Steam ID string
- `peer_control_marker`
  - short packets exposing markers such as `SS`, `KA`, `PP`, or `PR`
- `peer_state_block`
  - packets whose post-header body begins with `0x5f`, often with embedded unit
    strings like `bvtump`, `cvturr`, or `cvrremp`

This is still heuristic, but it is accurate enough to improve capture triage
and compatibility debugging without claiming a full field-by-field decode.

The current server tooling now uses these heuristics to summarize peer UDP by
remote endpoint, including:

- packet-family counts
- observed control markers
- named control-event counts
- Steam IDs and notable embedded strings
- phase sequence such as `identity -> control -> state`
- inferred stage such as `p2p_bootstrap` or `state_exchange`

### 9. Short Control Markers Now Have Useful Likely Meanings

The March 21 run is strong enough to promote several marker packets from raw
two-byte tags into named control events in tooling:

- `PP` -> likely `peer_ping_request`
  - high confidence
  - directly aligns with `BZLogger.txt` lines that print `PING SENT`
- `PR` -> likely `peer_ping_response`
  - high confidence
  - appears as the short paired response immediately after `PP`
- `SS` -> likely `peer_session_sync`
  - medium confidence
  - repeats throughout the bootstrap ramp before heavier state traffic
- `KA` -> likely `peer_keepalive`
  - medium confidence
  - very short two-byte marker seen after early sync exchange

Lower-confidence markers also exist:

- `PB`
  - short timing-sideband marker seen near `PP` and `PR`
- `PO`
- `PZ`

These should still be treated as heuristic labels, not final protocol names.
They are useful because status output can now describe probable intent instead
of only repeating raw marker bytes.

### 10. `PP`, `PR`, And `PB` Now Have Partial Field Sketches

The March 21 run supports a stronger decode for the timing-oriented short
control packets:

- `PP`
  - high confidence
  - the 20-byte body cleanly matches `BZLogger.txt` `PING SENT` fields:
    - `localMsTick`
    - `clockOffset`
    - `hostMsTick`
    - `delayUnits`
    - `flags`
- `PR`
  - medium confidence
  - the 12-byte body appears to be three little-endian words:
    - `echoedPingMsTick`
    - `responderMsTick`
    - `responseWord`
  - the first word matches the paired `PP.localMsTick`
- `PB`
  - medium confidence
  - the first little-endian word appears to track bandwidth pacing or target BW
  - this lines up with later `TempStateSendAll ... Target BW: 8590` style log
    entries, where `PB` packets carry first-word values like `8590`

The remaining `PR.responseWord` and most of `PB` beyond the first word are still
opaque. Tooling now surfaces them as structured fields so future captures can
narrow those meanings down without redoing the first-pass parsing work.

### 11. First `0x5f...` State Blocks Now Have A Header Sketch

The March 21 run also supports a practical first-pass decode for the earliest
`0x5f` state packets:

- bytes `2..5`
  - medium confidence
  - behave like a little-endian float clock in seconds
  - examples:
    - `21 b0 06 41` -> about `8.418`
    - `8e 97 c6 40` -> about `6.206`
- bytes `6..9`
  - medium confidence
  - stable per-stream state word
  - examples:
    - host stream often shows `99381`
    - peer stream often shows `100405`
- bytes `10..11`
  - medium confidence
  - clearly separate the two player streams in this run
  - observed as `1` for one side and `2` for the other
- bytes `12..15`
  - low confidence
  - changing header/control word that likely carries per-packet state flags or
    delta metadata

The same packets reliably expose short embedded unit codes, which are strong
triage markers:

- `cvturr`
- `cvremp`
- `bvtump`
- `bvremp`

Tooling now reports these as:

- `stateHeader`
- `unitCodes`
- `stateObjectCount`

This is still only a sketch, but it gives us a reusable way to compare state
streams over time and across peers.

### 12. `7a75...` Packets Are A Usable Object-Setup Family

The March 21 run also shows a distinct `7a75` packet family that sits between
early P2P setup and the denser `0x5f` state stream.

Current practical decode:

- `peer_object_descriptor`
  - medium confidence
  - these packets carry clear object metadata such as:
    - short object codes like `cvturr`, `cvremp`, `bvtump`, `bvremp`
    - longer object names like `cvturr0_turrettank` and `cvremp0_recycler`
  - the outer UDP wrapper also tells us whether the packet used reliable
    transport (`0xc0...`) or not (`0x40...`)
- `peer_object_control`
  - low confidence
  - shorter `7a75` packets without names still appear during the same roster or
    setup exchange, but their body fields are not yet fully explained

The current tooling now surfaces:

- `transportReliability`
- `objectHeader`
  - `objectVariant`
  - `objectIndex`
  - `descriptorFlags`
  - `objectHeaderWord`
  - `objectOwnerFlags`
  - `objectOwnerIndex`
- `objectCodes`
- `objectNames`
- `primaryObjectCode`
- `primaryObjectName`
- `objectNameStem`
- `objectKind`
- `objectOrdinal`
- `sideCodePrefix`
- `sideLabel`
- `personRole` for person-type descriptors
- `descriptorFamilyHint`
- `descriptorSketch`
  - `classWord`
  - `subtypeWord`
  - `ownerWord`
  - `controlWord`
  - `paramWord`
  - `vectorWordA`
  - `vectorWordB`
- `descriptorPattern`
- `descriptorWordProfile`
  - `classLowByte`
  - `classHighByte`
  - `subtypeLowByte`
  - `subtypeHighByte`
  - `ownerLowByte`
  - `ownerHighByte`
- `descriptorFieldHints`
  - `identityWords`
  - `payloadWords`
  - `ownerLane`
  - `stabilityAssessment`
- `descriptorArchetypeHint`
- `descriptorPayloadFamilyHint`
- `descriptorPayloadHints`
  - `controlLaneWords`
  - `vectorLaneWords`
  - `payloadLaneAssessment`
  - `controlSignature`

It also now separates several shorter setup variants into named object events:

- `object_roster_batch`
  - 31-byte batch header that precedes descriptor bursts
- `object_presence`
  - short `060601` packet with a `0x21xx` presence key
- `object_counter`
  - short `060601` packet carrying a synchronized counter value
- `object_setup_marker`
  - very short `0401` / `0402` setup marker

These now appear in protocol summaries as `udpObjectEvents` globally and
`objectSetupEvents` per peer session.

The descriptor layer is now strong enough to produce typed roster hints from
late `7a75` object names:

- `turrettank`
- `recycler`
- `scavenger`
- `wingman`
- `person`
- `scrap`

Examples from the March 21 run:

- `cvturr0_turrettank`
- `cvremp0_recycler`
- `cvscav2_scavenger`
- `cvfigh2_wingman`
- `bvscav2_scavenger`
- `bvraz2_wingman`
- `bsuser3_person`
- `bspilo3_person`
- `npscr10_scrap`

The numeric portion of those typed descriptors is now also surfaced as a raw
`descriptorPayloadHints` adds a more opinionated split across the later four
descriptor words:

- `controlLaneWords`
  - `controlWord`
  - `paramWord`
- `vectorLaneWords`
  - `vectorWordA`
  - `vectorWordB`
- `payloadLaneAssessment`
  - current heuristic: `controlWord` and `paramWord` look more family and
    side dependent, while `vectorWordA` and `vectorWordB` vary more like
    per-descriptor payload
- `controlSignature`
  - stable shorthand combining payload family and the two candidate control
    lane words, for example:
  - `c_side_structure_payload:0x00000000/0xf7000000`
  - `neutral_scrap_payload:0xdc000000/0x35000020`
  - `b_side_person_payload:0xd5ffa100/0xa84037a8`

Those `controlSignature` values are now aggregated in peer summaries as
`descriptorControlSignatures`, which gives a compact comparison surface for
matching payload-lane structure across descriptor families before we attempt a
deeper field-by-field decode.

Protocol summaries now also expose `descriptorPayloadComparisons`, which groups
all observed `peer_object_descriptor` packets by `descriptorPayloadFamilyHint`
and compares the later four payload words side by side. Each family summary now
includes:

- `payloadWords`
  - counted values for `controlWord`, `paramWord`, `vectorWordA`, and
    `vectorWordB`
- `stablePayloadFields`
- `varyingPayloadFields`
- `commonPayloadValues`
- `likelyControlFields`
- `likelyVectorFields`
- `payloadAssessment`
- `sampleDescriptors`
- `objectStablePayloadFields`
- `objectVaryingAcrossNamesFields`
- `objectSpecificPayloadFields`
- `mutableWithinObjectNameFields`
- `objectStabilityAssessment`
- `sampleObjectProfiles`
- `mutableObjectMutations`
- `payloadWordByteProfiles`
- `payloadWordHalfProfiles`
- `controlWordHalf0Clusters`
- `paramWordHalf1Clusters`
- `controlWordHalf0ClusterHints`
- `paramWordHalf1ClusterHints`

The first clear payoff from that comparison layer is the March 21
`b_side_mobile_unit_payload` family:

- both `bvscav2_scavenger` and `bvraz2_wingman` share `controlWord`
  `0xbd000000`
- their `paramWord`, `vectorWordA`, and `vectorWordB` all differ
- current best interpretation:
  - `controlWord` is a stronger family-level control/class field candidate
  - `paramWord` may still contain subtype or spawn-side control state
  - `vectorWordA` and `vectorWordB` remain the strongest per-descriptor payload
    or spawn/vector candidates

This comparison layer is now also usable offline against the full March 21
`BZLogger.txt` files via:

- `python analyze_bzlogger_descriptors.py --pretty <Ian BZLogger.txt> <Kateri BZLogger.txt>`

Across both March 21 logs, the extractor finds 83 `peer_object_descriptor`
packets and splits them into the same payload families seen in the live summary
surface. The strongest results so far are:

- `c_side_structure_payload`
  - 8 descriptors total
  - `controlWord = 0x00000000` and `paramWord = 0xf7000000` stay fixed
  - only `vectorWordA` and `vectorWordB` differ between `cvturr0_turrettank`
    and `cvremp0_recycler`
- `b_side_structure_payload`
  - 8 descriptors total
  - `controlWord = 0x00000000` and `paramWord = 0x99a00000` stay fixed
  - only the two vector words differ between `bvtump0_turrettank` and
    `bvremp0_recycler`
- `c_side_mobile_unit_payload`
  - 10 descriptors total
  - all observed scavenger and wingman descriptors share
    `controlWord = 0xda000000`
  - `paramWord`, `vectorWordA`, and `vectorWordB` split cleanly by
    `cvscav*` versus `cvfigh*`
- `b_side_mobile_unit_payload`
  - 10 descriptors total
  - all observed scavenger and wingman descriptors share
    `controlWord = 0xbd000000`
  - `paramWord`, `vectorWordA`, and `vectorWordB` split cleanly by
    `bvscav*` versus `bvraz*`
- `neutral_scrap_payload`
  - 39 descriptors total
  - all four payload words vary broadly across scrap descriptors
  - this currently looks more like per-object spawn/state data than a compact
    family control block
- `b_side_person_payload`
  - only 3 descriptors so far: `bsuser3_person`, `bspilo3_person`,
    `bsuser9_person`
  - all four payload words vary in this small sample, so more person captures
    are needed before naming stable lanes

The strongest current hypothesis after the full-log pass is:

- structure families probably use `controlWord + paramWord` as family-level
  control/class lanes while vector words carry object placement or descriptor-
  local payload
- mobile unit families probably use `controlWord` as a side/family lane, with
  `paramWord` already splitting by subtype (`scavenger` versus `wingman`)
- person payloads are not random noise: in the current March 21 logs all four
  payload words are stable within repeated named people and vary across named
  people, so they currently look object-specific rather than transient
- scrap payloads now split more cleanly than before:
  - `paramWord`, `vectorWordA`, and `vectorWordB` are stable within repeated
    named scrap objects and vary across scrap names
  - `controlWord` is mostly object-specific too, but at least one repeated
    object (`npscr111_scrap`) flips `0xaa000000` to `0xa9800000` while the
    other three payload words stay fixed
  - tooling now surfaces that mutation under `mutableObjectMutations`
  - current best guess: scrap `controlWord` carries an object-specific base
    plus at least one mutable state bit or flag lane

The current strongest example is:

- `npscr111_scrap`
  - observed `controlWord` values:
    - `0xaa000000`
    - `0xa9800000`
  - XOR delta from the lower value to the higher value:
    - `0x03800000`
  - changed bit positions:
    - `23`
    - `24`
    - `25`
  - `paramWord`, `vectorWordA`, and `vectorWordB` remain fixed through that
    flip

That makes bits `23..25` in scrap `controlWord` the first concrete candidate
state-flag lane inside the descriptor payload family.

The newer byte-lane summary (`payloadWordByteProfiles`) makes that sharper:

- `b_side_mobile_unit_payload.controlWord`
  - `hexByte0` is always `0xbd`
  - `hexByte1`, `hexByte2`, and `hexByte3` are always `0x00`
  - current interpretation: the family/control discriminator for this lane is
    concentrated in the top byte
- `neutral_scrap_payload.controlWord`
  - `hexByte2` and `hexByte3` are always `0x00`
  - only `hexByte0` and `hexByte1` vary
  - in the `npscr111_scrap` mutation case, the change is confined to those two
    leading bytes

So for the current March 21 scrap evidence, the lowest 16 bits of
`controlWord` look structurally unused or reserved, while the effective state
and identity signal appears to live in the upper 16 bits.

The halfword summary (`payloadWordHalfProfiles`) now makes that even more
explicit:

- `b_side_mobile_unit_payload.controlWord`
  - `hexHalf0` is always `0xbd00`
  - `hexHalf1` is always `0x0000`
- `neutral_scrap_payload.controlWord`
  - `hexHalf1` is always `0x0000` across all 39 observed descriptors
  - every observed variation, including the `npscr111_scrap` state flip, lives
    entirely in `hexHalf0`
  - the `npscr111_scrap` mutation now reports `changedHalfLanes = ["hexHalf0"]`

So the current best interpretation is narrower than before: for scrap
`controlWord`, the active lane is not just “the upper 16 bits” in general, but
specifically `hexHalf0`, with `hexHalf1` acting like a constant zero halfword
in the March 21 captures.

The newest cluster layer makes the family patterns easier to compare without
manual spreadsheet work:

- `controlWordHalf0Clusters`
  - groups descriptors by the active upper halfword of `controlWord`
  - shows which object names and ordinals share that lane plus the paired
    `paramWord` halves
- `paramWordHalf1Clusters`
  - groups descriptors by the lower halfword of `paramWord`
  - shows which control-half groups pair with that lower-half lane
- `controlWordHalf0ClusterHints`
  - classifies each upper-halfword cluster as `family_shared`,
    `multi_object_category`, or `single_object_specific`
- `paramWordHalf1ClusterHints`
  - does the same for the lower half of `paramWord`

This exposes a stronger mobile-unit pattern than we had before:

- `b_side_mobile_unit_payload`
  - `controlWordHalf0` is always `0xbd00`
  - `paramWordHalf1` is always `0x0022`
  - only the paired `paramWordHalf0` splits by subtype:
    - `0x4340` for observed `bvraz*` wingmen
    - `0xec80` for observed `bvscav*` scavengers
- `c_side_mobile_unit_payload`
  - `controlWordHalf0` is always `0xda00`
  - `paramWordHalf1` is always `0x0006`
  - only the paired `paramWordHalf0` splits by subtype:
    - `0xe780` for observed `cvfigh*` wingmen
    - `0x7f00` for observed `cvscav*` scavengers

That is currently our best field split for mobile-unit descriptor payloads:

- `controlWordHalf0`: side/family discriminator
- `paramWordHalf1`: family-wide lower-half constant
- `paramWordHalf0`: subtype lane that separates scavenger from wingman

Scrap remains looser:

- `controlWordHalf0Clusters` mostly map one upper-halfword to one named scrap
  object
- `paramWordHalf1Clusters` do repeat across different scrap names, so scrap
  `paramWordHalf1` currently looks less like a unique object identifier and
  more like a smaller category/state lane

The new cluster hints make that distinction explicit:

- `b_side_mobile_unit_payload`
  - `controlWordHalf0ClusterHints["0xbd00"] = family_shared`
  - `paramWordHalf1ClusterHints["0x0022"] = family_shared`
- `c_side_mobile_unit_payload`
  - `controlWordHalf0ClusterHints["0xda00"] = family_shared`
  - `paramWordHalf1ClusterHints["0x0006"] = family_shared`
- `neutral_scrap_payload`
  - `paramWordHalf1ClusterHints["0x0020"] = multi_object_category`
    - currently spans `npscr10_scrap`, `npscr39_scrap`, and `npscr112_scrap`
  - `paramWordHalf1ClusterHints["0x0034"] = multi_object_category`
    - currently spans `npscr24_scrap` and `npscr30_scrap`
  - `controlWordHalf0ClusterHints["0x3580"] = multi_object_category`
    - currently spans `npscr29_scrap` and `npscr31_scrap`
  - `paramWordHalf1ClusterHints["0x0008"] = single_object_specific`
    - stays tied to `npscr111_scrap`, which is also the only currently observed
      mutable scrap control-word case

I also added vector-alignment summaries:

- `controlWordHalf0VectorClusters`
- `controlWordHalf0VectorClusterHints`
- `paramWordHalf1VectorClusters`
- `paramWordHalf1VectorClusterHints`

Those make the current scrap result sharper:

- `paramWordHalf1 = 0x0020`
  - spans `npscr10_scrap`, `npscr39_scrap`, and `npscr112_scrap`
  - `pairedVectorAHalf1` splits between `0x0002` and `0x1fc5`
  - `pairedVectorBHalf0` splits between `0x6e0a` and `0x6e10`
- `paramWordHalf1 = 0x0034`
  - spans `npscr24_scrap` and `npscr30_scrap`
  - also splits across both `vectorAHalf1` and `vectorBHalf0`
- `controlWordHalf0 = 0x3580`
  - spans `npscr29_scrap` and `npscr31_scrap`
  - again splits across both `vectorAHalf1` and `vectorBHalf0`

So the current best negative result is:

- shared scrap `paramWordHalf1` and `controlWordHalf0` category candidates do
  not map cleanly to one vector signature
- they currently look more like shared descriptor category/state lanes than
  simple position buckets

The new summary fields surface that directly as:

- `paramWordHalf1VectorAssessment = "multi-object paramWordHalf1 clusters span
  multiple vector signatures; not a simple vector bucket"`
- `controlWordHalf0VectorAssessment = "multi-object controlWordHalf0 clusters
  span multiple vector signatures; not a simple vector bucket"`

I also added a descriptor/state correlation layer:

- `descriptorStateCorrelations`
- `descriptorFollowups`
- offline extractor: `analyze_bzlogger_state_correlations.py`

On the full March 21 logs, that summary currently shows:

- every observed `0x5f` state-stream unit code also appears earlier in the
  `7a75` descriptor stream
- the shared overlap is the combat/structure subset:
  - `bvtump`
  - `bvremp`
  - `cvturr`
  - `cvremp`
  - `cvscav`
  - `cvfigh`
- descriptor-only leftovers include:
  - person codes like `csuser`, `bsuser`, `bspilo`
  - scrap codes like `npscr`, `npscr1`, `npscr2`, `npscr3`
  - some side-specific roster codes such as `bvraz` and `bvscav`

So the current best correlation result is:

- `0x5f` state blocks reuse a subset of the earlier descriptor roster
- people and scrap remain descriptor-only in this run
- the scrap halfword categories we isolated above do not yet have a direct
  state-block counterpart

The new `descriptorFollowups` summary adds one more useful split inside the
descriptor-only side of the traffic:

- `person`
  - `descriptorCount = 8`
  - `firstShortObjectEvents`
    - `object_presence = 4`
    - `object_counter = 2`
  - so people often do receive a nearby short `7a75` setup/control followup
- `scrap`
  - `descriptorCount = 39`
  - `firstShortObjectEvents = {}`
  - `firstNonDescriptorEvents`
    - `peer_state_block = 34`
    - `peer_control_unknown_po = 4`
    - `peer_identity = 1`
  - so scrap descriptors mostly hand off directly into the broader peer stream
    without a decoded short `7a75` followup

That gives us a better working split:

- people are descriptor-only with respect to `0x5f`, but they still participate
  in some nearby short-object followups
- scrap is descriptor-only with respect to `0x5f` and currently also lacks a
  decoded short-object followup path in the same immediate window

I also promoted the previously unresolved `PO` and generic
`peer_object_control` traffic into a partial control summary:

- `unresolvedControlComparisons`

For `PO`, the payload now exposes:

- `clockSeconds`
- `modeWord`
- `sequenceValue`
- `laneValue`
- `nearbyDescriptorKinds`
- `nearbyObjectEvents`
- `nearbyObjectNames`
- `nearbyScrapControlHalf0`
- `nearbyScrapParamHalf1`
- `nearbyMutableScrapNames`
- `nearbyMutableScrapControlHalf0`
- `mutableScrapAssessment`

That already separates the March 21 traffic into two stable families:

- `0x0306/lane0`
  - `count = 40`
  - `sequenceValues = 61..80`
  - `clockSecondsRange = 53.379 .. 149.794`
  - `nearbyObjectEvents`
    - `object_counter = 160`
- `0x0206/lane2`
  - `count = 5`
  - `sequenceValues = 11..15`
  - `clockSecondsRange = 179.346 .. 179.346`
  - `nearbyDescriptorKinds`
    - `scrap = 16`
  - `nearbyObjectNames`
    - `npscr110_scrap = 4`
    - `npscr29_scrap = 4`
    - `npscr111_scrap = 3`
    - `npscr39_scrap = 3`
    - `npscr112_scrap = 2`
  - `nearbyScrapControlHalf0`
    - `0x3580 = 4`
    - `0x6100 = 4`
    - `0xa980 = 3`
    - `0x6d80 = 3`
    - `0x0a80 = 2`
  - `nearbyScrapParamHalf1`
    - `0x0020 = 5`
    - `0x000b = 4`
    - `0x0022 = 4`
    - `0x0008 = 3`
  - `nearbyMutableScrapNames`
    - `npscr111_scrap = 3`
  - `nearbyMutableScrapControlHalf0`
    - `0xa980 = 3`
  - `mutableScrapAssessment`
    - overlaps mutable scrap via `npscr111_scrap`
    - observed mutable scrap controlHalf0 is `0xa980`
  - `sequenceNextDescriptorNames`
    - `11 -> npscr39_scrap`
    - `12 -> npscr29_scrap`
    - `13 -> npscr110_scrap`
    - `14 -> npscr111_scrap`
    - `15 -> npscr112_scrap`
  - `sequenceNextScrapControlHalf0`
    - `11 -> 0x6d80`
    - `12 -> 0x3580`
    - `13 -> 0x6100`
    - `14 -> 0xa980`
    - `15 -> 0x0a80`
  - `sequenceNextScrapParamHalf1`
    - `11 -> 0x0020`
    - `12 -> 0x000b`
    - `13 -> 0x0022`
    - `14 -> 0x0008`
    - `15 -> 0x0020`
  - `sequenceNextScrapOrdinals`
    - `11 -> 39`
    - `12 -> 29`
    - `13 -> 110`
    - `14 -> 111`
    - `15 -> 112`
  - `sequenceNextScrapSubtypeWords`
    - `11 -> 0x0b00`
    - `12 -> 0x0c00`
    - `13 -> 0x0d00`
    - `14 -> 0x0e00`
    - `15 -> 0x0f00`
  - `sequencePatternAssessment`
    - next-scrap `subtypeWord` steps contiguously while `objectOrdinal` does not
  - `subtypeToScrapControlHalf0`
    - `0x0b00 -> 0x6d80`
    - `0x0c00 -> 0x3580`
    - `0x0d00 -> 0x6100`
    - `0x0e00 -> 0xa980`
    - `0x0f00 -> 0x0a80`
  - `subtypeToScrapParamHalf1`
    - `0x0b00 -> 0x0020`
    - `0x0c00 -> 0x000b`
    - `0x0d00 -> 0x0022`
    - `0x0e00 -> 0x0008`
    - `0x0f00 -> 0x0020`
  - `sequenceHalfwordAssessment`
    - `subtypeWord` maps one-to-one onto `controlHalf0`, while `paramHalf1`
      repeats across subtype steps

So `PO` is no longer just “unknown marker”; it currently looks like a short
object-side timing/control frame with:

- a float-like elapsed-time field
- a dominant `0x0306/lane0` cadence
- the dominant cadence sitting near `object_counter`
- a smaller late `0x0206/lane2` burst
- the late burst clustering only around scrap descriptors in the current local
  windowed context
- that late burst concentrating around the specific scrap cluster
  `npscr110`, `npscr29`, `npscr111`, `npscr39`, and `npscr112`
- that same burst overlapping the only currently mutable scrap object,
  `npscr111_scrap`, via its mutated `0xa980` upper-halfword
- and not overlapping the earlier `0xaa00` variant in the same local-control
  view
- with a stable late-burst interleave:
  `PO11 -> npscr39`, `PO12 -> npscr29`, `PO13 -> npscr110`,
  `PO14 -> npscr111`, `PO15 -> npscr112`
- with that same interleave walking contiguous scrap subtype words
  `0x0b00 -> 0x0c00 -> 0x0d00 -> 0x0e00 -> 0x0f00`
- while the linked scrap ordinals jump `39, 29, 110, 111, 112`, so the late
  burst does not look like a simple ascending ordinal sweep
- and with that subtype ladder mapping cleanly onto distinct `controlHalf0`
  values, while `paramHalf1` is looser and repeats (`0x0020` appears at both
  `0x0b00` and `0x0f00`)

The newer `descriptorPayloadComparisons.subtypeWordClusters` view adds an
important broader-context correction to that late-burst picture. Across the
full March 21 scrap population, several of those subtype words are reused by
more than one named scrap object:

- `0x0c00`
  - `npscr29_scrap -> 0x3580 / 0x000b`
  - `npscr34_scrap -> 0x9000 / 0x003e`
- `0x0d00`
  - `npscr110_scrap -> 0x6100 / 0x0022`
  - `npscr24_scrap -> 0xcd00 / 0x0034`
- `0x0e00`
  - `npscr111_scrap -> 0xaa00 or 0xa980 / 0x0008`
  - `npscr35_scrap -> 0x0f00 / 0x0011`
- `0x0f00`
  - `npscr112_scrap -> 0x0a80 / 0x0020`
  - `npscr36_scrap -> 0x8080 / 0x0024`

So the current best interpretation is:

- `subtypeWord` looks more like a scrap family/category lane than a unique
  object id
- the late `PO 0x0206/lane2` burst is walking one branch of that subtype
  family space
- `controlHalf0` and `paramHalf1` then act as per-branch or per-object
  overlays within each reused subtype family

The new `subtypeMutationCorrelations` view sharpens that again:

- `mutableSubtypeWords`
  - only `0x0e00`
- `0x0e00`
  - object names: `npscr111_scrap`, `npscr35_scrap`
  - mutable object names: `npscr111_scrap`
  - mutable field: `controlWord`
  - mutable control-halfword values stay inside the `npscr111_scrap` branch:
    `0xaa00` and `0xa980`
  - mutable param-halfword stays `0x0008`

So the current best mutation interpretation is:

- `0x0e00` is currently the only subtype family with an observed mutable object
- but the mutation still looks object-specific to `npscr111_scrap`, not
  subtype-wide across all `0x0e00` scrap descriptors

The new `mutableObjectObservationContexts` view clarifies the exact packet-order
shape of that mutation:

- `npscr111_scrap` with `0xaa00 / 0x0008`
  - immediate previous event: `npscr110_scrap`
  - immediate next event: `npscr112_scrap`
  - no immediate short-control marker on either side
- `npscr111_scrap` with `0xa980 / 0x0008`
  - immediate previous event: `peer_control_unknown_po`
  - immediate next event: `peer_control_unknown_po`
  - the mutated observation is bracketed by `PO` on both sides

So the current best transition interpretation is:

- the `0xaa00 -> 0xa980` flip does not look like a generic repeated `0x0e00`
  refresh
- the `0xa980` observation is the only `npscr111_scrap` state currently seen in
  a direct `PO -> descriptor -> PO` sandwich

The broader `objectObservationContextSignatures` view adds one important
correction to that: the late-burst `PO` sandwich is not unique to
`npscr111_scrap`.

Across the combined March 21 logs:

- `npscr39_scrap`
  - `no_short_control = 2`
  - `po_sandwiched = 1`
- `npscr29_scrap`
  - `no_short_control = 2`
  - `po_sandwiched = 1`
- `npscr110_scrap`
  - `no_short_control = 2`
  - `po_sandwiched = 1`
- `npscr111_scrap`
  - `no_short_control = 2`
  - `po_sandwiched = 1`
- `npscr112_scrap`
  - `no_short_control = 2`
  - `po_leading_edge = 1`

So the late refresh burst now looks like a broader alternating pattern:

- `npscr39`, `npscr29`, `npscr110`, and `npscr111` each get one
  `PO -> descriptor -> PO` observation
- `npscr112` appears at the trailing edge with a leading `PO` but no captured
  trailing neighbor in the current log slice
- `npscr111_scrap` remains special not because it is the only descriptor inside
  that pattern, but because it is the only one inside the pattern that also
  carries the observed mutable `0xaa00 -> 0xa980` control-halfword flip

The new `poContextControlComparisons` summary makes that last point explicit:

- `npscr39_scrap`
  - role: `po_sandwiched`
  - `controlHalf0 = 0x6d80` across all 3 observed descriptors
- `npscr29_scrap`
  - role: `po_sandwiched`
  - `controlHalf0 = 0x3580` across all 3 observed descriptors
- `npscr110_scrap`
  - role: `po_sandwiched`
  - `controlHalf0 = 0x6100` across all 3 observed descriptors
- `npscr111_scrap`
  - role: `po_sandwiched`
  - `controlHalf0 = 0xaa00` twice and `0xa980` once
  - mutable field: `controlWord`
- `npscr112_scrap`
  - role: `po_leading_edge`
  - `controlHalf0 = 0x0a80` across all 3 observed descriptors

So the current best comparison is:

- among the `PO`-sandwiched scrap descriptors, only `npscr111_scrap` shows
  multiple observed control-halfword states
- `npscr39`, `npscr29`, and `npscr110` currently remain single-state
- `npscr112` still looks like the trailing edge of the burst, not another
  multi-state sandwiched case

The new `mutableObjectStateTimelines` summary makes the remaining ambiguity much
smaller:

- `npscr111_scrap`
  - `controlHalf0Timeline = [0xaa00, 0xaa00, 0xa980]`
  - `uniqueControlHalf0Sequence = [0xaa00, 0xa980]`
  - `finalObservedControlHalf0 = 0xa980`
  - `reverseTransitionObserved = false`
  - observed time range:
    `2026-03-21 13:15:35.376028` through `2026-03-21 13:15:45.301753`

So the current best state-change interpretation is:

- `npscr111_scrap` currently looks like a one-way observed transition from
  `0xaa00` into `0xa980`
- no later reverse `0xa980 -> 0xaa00` transition is visible anywhere in the
  current March 21 logs

The new `mutableObjectStateTransitions` summary also sharpens the likely trigger
context around that change:

- observed transition
  - `0xaa00 -> 0xa980`
  - previous observation:
    `2026-03-21 13:15:35.987732` at Ian log line `40555`
  - changed observation:
    `2026-03-21 13:15:45.301753` at Ian log line `42439`
- immediate neighbors at the changed observation
  - previous event: `peer_control_unknown_po`
  - next event: `peer_control_unknown_po`
- preceding 8-packet local window
  - `peer_control_marker = 4`
  - `peer_object_descriptor = 3`
  - `peer_state_block = 1`
  - no `peer_object_control`
  - preceding descriptor names:
    `npscr39_scrap`, `npscr29_scrap`, `npscr110_scrap`
- nearest preceding state block
  - time: `2026-03-21 13:15:45.257708`
  - line: `42406`
  - header:
    `stateTimeSeconds = 179.305`,
    `stateStreamWord = 525362`,
    `statePlayerIndex = 1`,
    `stateHeaderWord = 0x59967c18`
  - unit codes:
    `cvscav`, `cvfigh`, `cvremp`
  - `stateObjectCount = 3`

So the current best trigger interpretation is:

- `npscr111_scrap` changes state inside the late `PO` burst
- that change happens after recent `peer_state_block` traffic
- the last preceding state block only exposes `cvscav`, `cvfigh`, and `cvremp`,
  not any scrap-visible unit code
- and there is still no nearby `peer_object_control` evidence that the shorter
  `7a75` control packets are the direct trigger for the observed `0xaa00 ->
  0xa980` flip

The new `mutableObjectPostTransitionRevisits` view also narrows the persistence
question:

- after the changed `npscr111_scrap` observation at line `42439`
  - `postTransitionPacketCount = 2`
  - `followingEventFamilies`
    - `peer_control_marker = 1`
    - `peer_object_descriptor = 1`
  - `followingObjectNames`
    - `npscr112_scrap = 1`
  - `laterSameObjectControlHalf0`
    - none

So the current best persistence interpretation is:

- there is no later `npscr111_scrap` descriptor revisit after the observed
  `0xa980` state in the current March 21 log tail
- the remaining captured tail only shows the closing `PO` marker plus
  `npscr112_scrap`
- so `0xa980` is still the final observed state for `npscr111_scrap` in this
  run, not a state that is later re-sampled or reversed before capture end

The new raw-pcap extractor in `analyze_pcapng_udp.py` now confirms that this is
not just a `BZLogger.txt` artifact. Running:

```bash
python analyze_pcapng_udp.py --object npscr111_scrap --window 4 --pretty "...\game1.pcapng" "...\Kat_PC.pcapng"
```

on the March 21, 2026 captures shows the same local transition in both raw
pcaps:

- `npscr111_scrap`
  - `0xaa00 -> 0xa980`
  - subtype stays `0x0e00`
  - `paramWord` stays `0x33000008`
- the nearest preceding state block is the same in both captures:
  - `stateTimeSeconds = 179.305`
  - `stateStreamWord = 525362`
  - `statePlayerIndex = 1`
  - `stateHeaderWord = 0x59967c18`
  - unit codes: `cvscav`, `cvfigh`, `cvremp`
- the changed observation sits in the same seven-packet local window in both
  pcaps:
  - `npscr29_scrap`
  - `PO`
  - `npscr110_scrap`
  - `PO`
  - `npscr111_scrap`
  - `PO`
  - `npscr112_scrap`
- both raw captures also end with only two packets after the changed
  `npscr111_scrap` observation

That makes the current interpretation stronger: the late scrap transition is
visible in synchronized raw packet captures from both PCs, and the surrounding
`PO -> descriptor -> PO` rhythm matches the `BZLogger.txt` analysis exactly.

The same raw-pcap tool now also supports multiple `--object` filters, which
lets us compare the neighboring late-burst scrap objects directly:

```bash
python analyze_pcapng_udp.py --object npscr29_scrap --object npscr110_scrap --object npscr111_scrap --object npscr112_scrap --window 4 --pretty "...\game1.pcapng" "...\Kat_PC.pcapng"
```

That comparison narrows the result further. In both raw captures:

- `npscr29_scrap`
  - observations: `2`
  - subtype: `0x0c00`
  - `controlHalf0`: always `0x3580`
  - `paramHalf1`: always `0x000b`
- `npscr110_scrap`
  - observations: `2`
  - subtype: `0x0d00`
  - `controlHalf0`: always `0x6100`
  - `paramHalf1`: always `0x0022`
- `npscr111_scrap`
  - observations: `2`
  - subtype: `0x0e00`
  - `controlHalf0`: `0xaa00 -> 0xa980`
  - `paramHalf1`: always `0x0008`
- `npscr112_scrap`
  - observations: `2`
  - subtype: `0x0f00`
  - `controlHalf0`: always `0x0a80`
  - `paramHalf1`: always `0x0020`

So the late-burst pcap comparison now matches the best log-derived conclusion:
among the four late-burst scrap descriptors that bracket the observed
transition, only `npscr111_scrap` currently shows a real two-state control
history. The neighboring objects look stable across both synchronized captures.

Widening the same raw-pcap comparison to the other subtype-family neighbors
sharpens that result a bit more:

```bash
python analyze_pcapng_udp.py --object npscr24_scrap --object npscr34_scrap --object npscr35_scrap --object npscr36_scrap --object npscr110_scrap --object npscr111_scrap --object npscr112_scrap --pretty "...\game1.pcapng" "...\Kat_PC.pcapng"
```

Across both raw captures:

- subtype `0x0d00`
  - `npscr24_scrap` appears once with `controlHalf0 = 0xcd00`, `paramHalf1 = 0x0034`
  - `npscr110_scrap` appears twice and stays `controlHalf0 = 0x6100`, `paramHalf1 = 0x0022`
- subtype `0x0e00`
  - `npscr35_scrap` appears once with `controlHalf0 = 0x0f00`, `paramHalf1 = 0x0011`
  - `npscr111_scrap` appears twice and changes `0xaa00 -> 0xa980` while keeping `paramHalf1 = 0x0008`
- subtype `0x0f00`
  - `npscr36_scrap` appears once with `controlHalf0 = 0x8080`, `paramHalf1 = 0x0024`
  - `npscr112_scrap` appears twice and stays `controlHalf0 = 0x0a80`, `paramHalf1 = 0x0020`

So the current best interpretation improves again:

- `npscr111_scrap` is still the only subtype-`0x0e00` object with an observed
  two-state control history in the current synchronized raw captures
- but `npscr35_scrap` is only sampled once, so the raw-pcap data still does not
  prove that `npscr111_scrap` is uniquely mutable across the entire subtype
  family in all runs
- what it does prove is narrower and still valuable: inside this March 21, 2026
  session, `npscr111_scrap` is the only observed subtype-`0x0e00` object that
  actually transitions

The raw-pcap tool now also has a subtype-wide mode:

```bash
python analyze_pcapng_udp.py --subtype 0x0e00 --pretty "...\game1.pcapng" "...\Kat_PC.pcapng"
```

This gives the same conclusion in a more compact form. In both raw captures:

- subtype `0x0e00` contains exactly:
  - `npscr111_scrap`
  - `npscr35_scrap`
- `npscr111_scrap`
  - observations: `2`
  - `controlHalf0`: `0xaa00 -> 0xa980`
  - `paramHalf1`: always `0x0008`
- `npscr35_scrap`
  - observations: `1`
  - `controlHalf0`: `0x0f00`
  - `paramHalf1`: `0x0011`
- subtype-wide assessment:
  - `subtype 0x0e00 has observed multi-state objects: npscr111_scrap; single-sample objects remain: npscr35_scrap`

So the subtype-level raw-capture answer is now explicit:

- in this March 21, 2026 session, subtype `0x0e00` does include an observed
  multi-state object
- that multi-state object is `npscr111_scrap`
- `npscr35_scrap` remains the key unresolved subtype peer because it is still
  only sampled once per capture

Running the same subtype-wide view across the neighboring scrap subtype ladder:

```bash
python analyze_pcapng_udp.py --subtype 0x0c00 --subtype 0x0d00 --subtype 0x0e00 --subtype 0x0f00 --pretty "...\game1.pcapng" "...\Kat_PC.pcapng"
```

now gives the cleanest March 21, 2026 subtype comparison so far:

- subtype `0x0c00`
  - objects: `npscr29_scrap`, `npscr34_scrap`
  - assessment:
    `subtype 0x0c00 shows no observed multi-state objects in this capture;
    single-sample objects remain: npscr34_scrap`
- subtype `0x0d00`
  - objects: `npscr110_scrap`, `npscr24_scrap`
  - assessment:
    `subtype 0x0d00 shows no observed multi-state objects in this capture;
    single-sample objects remain: npscr24_scrap`
- subtype `0x0e00`
  - objects: `npscr111_scrap`, `npscr35_scrap`
  - assessment:
    `subtype 0x0e00 has observed multi-state objects: npscr111_scrap;
    single-sample objects remain: npscr35_scrap`
- subtype `0x0f00`
  - objects: `npscr112_scrap`, `npscr36_scrap`
  - assessment:
    `subtype 0x0f00 shows no observed multi-state objects in this capture;
    single-sample objects remain: npscr36_scrap`

So the strongest current raw-capture interpretation is:

- among the observed scrap subtype ladder `0x0c00 .. 0x0f00`, only subtype
  `0x0e00` currently contains any observed multi-state object
- that multi-state object is still only `npscr111_scrap`
- the main limitation is unchanged: each neighboring subtype still contains one
  single-sample peer object, so this is strong run-specific evidence rather than
  a final universal claim about all future captures

The updated raw-pcap tool now also emits a cross-pcap consensus block, so the
two synchronized perspectives can be compared directly instead of side by side.
For the March 21, 2026 subtype ladder, the consensus result is:

- `0x0c00`
  - all pcaps agree no mutable objects were observed
  - all pcaps agree the single-sample object is `npscr34_scrap`
  - all pcaps agree the subtype roster is `npscr29_scrap`, `npscr34_scrap`
- `0x0d00`
  - all pcaps agree no mutable objects were observed
  - all pcaps agree the single-sample object is `npscr24_scrap`
  - all pcaps agree the subtype roster is `npscr110_scrap`, `npscr24_scrap`
- `0x0e00`
  - all pcaps agree the mutable object is `npscr111_scrap`
  - all pcaps agree the single-sample object is `npscr35_scrap`
  - all pcaps agree the subtype roster is `npscr111_scrap`, `npscr35_scrap`
- `0x0f00`
  - all pcaps agree no mutable objects were observed
  - all pcaps agree the single-sample object is `npscr36_scrap`
  - all pcaps agree the subtype roster is `npscr112_scrap`, `npscr36_scrap`

So the strongest statement we can currently make from synchronized raw packet
data is narrower but firmer:

- both March 21, 2026 raw captures agree that `0x0e00` is the only observed
  scrap subtype in this run with a mutable object
- both raw captures agree that the mutable object is `npscr111_scrap`

To make future run-to-run comparison easier, the raw-pcap tool now also supports
compact consensus export:

```bash
python analyze_pcapng_udp.py --subtype 0x0c00 --subtype 0x0d00 --subtype 0x0e00 --subtype 0x0f00 --consensus-only --pretty --write subtype_consensus_20260321.json "...\game1.pcapng" "...\Kat_PC.pcapng"
```

That command writes a minimal JSON artifact containing just:

- selected `pcaps`
- selected `subtypeWords`
- `objectConsensusReports`
- `subtypeConsensusReports`

For this run it produced `subtype_consensus_20260321.json`, which is now a much
better baseline artifact for future capture comparisons than a full packet dump.

I also added a small comparison tool:

```bash
python compare_capture_consensus.py --pretty subtype_consensus_20260321.json NEW_RUN_CONSENSUS.json
```

That comparer focuses on:

- `subtypeConsensusDiff`
- `objectConsensusDiff`

and reports:

- added keys
- removed keys
- changed fields inside matching subtype/object consensus entries
- unchanged counts

As a sanity check, comparing the March 21 baseline artifact to itself returns:

- `identical = true`
- `summary = "baseline and candidate consensus artifacts match exactly"`
- `subtypeConsensusDiff.changed = []`

So the workflow for the next run is now straightforward:

1. Export a fresh consensus JSON with `analyze_pcapng_udp.py --consensus-only`.
2. Compare it against `subtype_consensus_20260321.json`.
3. Inspect only the subtype/object deltas instead of rereading full capture
   summaries.

The comparison tool now also supports human-readable markdown output:

```bash
python compare_capture_consensus.py --markdown --write subtype_consensus_20260321_compare_self.md subtype_consensus_20260321.json subtype_consensus_20260321.json
```

That mode produces a short report with:

- baseline path
- candidate path
- overall summary
- subtype diff counts
- object diff counts
- field-level changes only when they actually exist

As a quick sanity check on the formatting path, the self-compare markdown report
shows:

- `Summary: baseline and candidate consensus artifacts match exactly`
- `Subtype Diff`
  - `Changed: 0`
  - `Unchanged: 4`
- `Object Diff`
  - `Changed: 0`
  - `Unchanged: 0`

The newer consensus/export workflow is also now pinned by the local smoke test
suite. The current decoder-side regression coverage includes:

- consensus-only summary compaction
- identical consensus JSON comparison
- changed consensus JSON comparison with field-level diff checks
- markdown report rendering for changed subtype/object entries

So the new run-processing workflow is no longer just manual tooling; it now has
basic regression protection too.

I also added a one-command workflow wrapper:

```bash
python run_capture_consensus_workflow.py --pretty --output-prefix march21_workflow --baseline-consensus subtype_consensus_20260321.json "...\game1.pcapng" "...\Kat_PC.pcapng"
```

By default that wrapper uses the current scrap subtype ladder:

- `0x0c00`
- `0x0d00`
- `0x0e00`
- `0x0f00`

and writes:

- `march21_workflow_consensus.json`
- `march21_workflow_compare.json`
- `march21_workflow_compare.md`

On the current March 21, 2026 baseline self-check, the wrapper reports:

- `summary = baseline and candidate consensus artifacts match exactly`

So the next-run process is now even shorter:

1. point the wrapper at the new `.pcapng` pair
2. provide the March 21 baseline consensus JSON
3. read the generated markdown diff first, then drill into JSON only if needed

Generic `peer_object_control` packets are also now surfaced by stable
signatures. In this run there are only four:

- `01001000000000000403000000`
  - `count = 2`
  - `controlModeWord = 0x0304`
- `01001000000000000404000000`
  - `count = 2`
  - `controlModeWord = 0x0404`
- `0100af210000000006060507000200`
  - `count = 2`
  - `controlModeWord = 0x0606`
  - `controlTailWord = 0x07050606`
  - `nearbyObjectEvents`
    - `object_setup_marker = 1`
- `0200af210000000006060507000200`
  - `count = 2`
  - `controlModeWord = 0x0606`
  - `controlTailWord = 0x07050606`
  - `nearbyObjectEvents`
    - `object_setup_marker = 2`

That still leaves semantics open, but it narrows the problem cleanly:

- `PO` has a real clock/mode/sequence structure now
- `peer_object_control` is a tiny repeated signature set, not arbitrary noise

So the current best interpretation is:

- mobile-unit lower-half `paramWord` values are family-wide constants
- mobile-unit upper-half `paramWord` values carry subtype
- scrap `paramWordHalf1` sometimes behaves like a shared category/state lane
- scrap `controlWordHalf0` is usually object-specific, but a few repeated upper
  halves already look like small category groups rather than unique IDs

The numeric portion of those typed descriptors is now also surfaced as a raw
`descriptorSketch`. These words repeat consistently by descriptor family, but
they are still best treated as partially decoded class/control fields rather
than confirmed world coordinates.

Tooling also now aggregates descriptor signatures as `descriptorPattern` values
such as:

- `0x2f2f/0x0184/0x0100`
- `0x2d2d/0x0204/0x0100`
- `0x2b2b/0x0708/0x0200`

That gives us a stable way to compare descriptor families across peers and runs
before every word has a final semantic label.

The current tooling also splits descriptor words into:

- `identityWords`
  - `classWord`
  - `subtypeWord`
  - `ownerWord`
- `payloadWords`
  - `controlWord`
  - `paramWord`
  - `vectorWordA`
  - `vectorWordB`

This is still heuristic, but it matches the current evidence well:

- the identity words stay stable by descriptor family
- the later payload words vary more across concrete descriptors of the same
  broad family

On top of that, the current tooling derives higher-level family hints from code
prefix plus typed object kind, for example:

- `c_side_turrettank_descriptor`
- `neutral_scrap_descriptor`
- `b_side_person_pilot_descriptor`

These are intentionally heuristic labels, but they make multi-packet summaries
easier to scan than raw pattern words alone.

The current tooling also derives:

- side-independent archetypes such as:
  - `turrettank`
  - `scrap`
  - `person_pilot`
- payload-family hints such as:
  - `c_side_structure_payload`
  - `b_side_mobile_unit_payload`
  - `b_side_person_payload`
  - `neutral_scrap_payload`

These are still heuristic, but they give us a cleaner way to compare the
volatile descriptor payload words across broad object families.

This is enough to separate object roster traffic from later state replication
and to compare what each peer believes exists before `0x5f` state updates ramp
up.

## Practical Construction Notes

These findings are strong enough to guide the next compatibility steps:

1. The UDP `1338` shim should reply with `probe + 0x04 + observed_ipv4 +
   observed_port_be`.
2. The WebSocket shim should keep `DoP2PConnect` / `OnLobbyMemberP2PConnect`
   semantics fast and symmetric for both peers.
3. The implementation should tolerate peer UDP arriving before the full WS
   signaling sequence has visibly settled on both clients.
4. Same-LAN runs still exercise the official control plane, but the peer UDP
   path prefers LAN addresses once handoff begins.
5. Status tooling should expose both raw markers and named control events so
   future captures can confirm or falsify the current interpretations.
6. Timing-oriented markers should expose structured field guesses in capture
   summaries, especially for `PP`, `PR`, and `PB`.
7. Early `0x5f` state packets should expose a stable header sketch and unit-code
   summary even before we fully decode their per-object bodies.
8. The `7a75` object-setup family should stay split from generic unknown UDP so
   roster descriptors and names are visible in summaries.

## Next Decode Targets

The most promising next packet-construction targets are:

1. The short `0x4003` / `0x4004` / `0x4005` Steam-ID packets used at first
   direct contact.
2. The small follow-up control packets containing `SS`, `KA`, `PP`, and `PR`
   markers.
3. The first post-launch `0x5f...` state packets that carry the initial object
   roster and unit identifiers.
