// ONE PHASE. Everything ships. 6 waves, 22 working days.
// Connectors are CODED against cassettes in-wave; real credentials are wired at go-live (W5).
module.exports = {
  START:"2026-09-08", TRACK_CAP:10, LEAVES_PER_TRACK_DAY:14, INTEGRATION_DAYS:1,
  // modules large enough to run as two or three concurrent tracks
  SPLIT:{M39:2,M40:2,M32:2,M13:2,M37:3,M11:2,M14:2,M33:2,M30:2,M19:2,M27:2,M7:2,M16:2,M17:2},
  WAVE:{
    // W0 foundation — everything types against this
    M0:0, M31:0, M38:0,
    // W1 the gate — the one wave that is not compressed
    M1:1, M2:1, M3:1, M4:1, M24:1, M5:1,
    // W2 data, channels, retrieval — connectors coded against cassettes
    M32:2, M11:2, M10:2, M9:2, M12:2, M15:2, M7:2, M22:2, M23:2, M8:2,
    // W3 agents, knowledge, memory, console
    M13:3, M39:3, M20:3, M6:3, M28:3, M14:3, M16:3, M27:3, M33:3,
    // W4 doing, lifecycle, extensions
    M17:4, M40:4, M18:4, M21:4, M25:4, M26:4, M29:4, M34:4, M35:4,
    // W5 hands, delivery, scale, go-live
    M19:5, M30:5, M36:5, M37:5
  },
  NAMES:{
    0:"Foundation", 1:"The gate", 2:"Data, channels, retrieval",
    3:"Agents, knowledge, console", 4:"Doing, lifecycle, extensions",
    5:"Hands, delivery, go-live"
  }
};
