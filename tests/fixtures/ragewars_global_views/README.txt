RageWars cleanup4 global views, b278c2985017 publication route.
D_80140F80 has retained scalar, byte and structured declarations in distinct
native consumer views. The fixtures retain exact object declarations and
complete menu/replay layouts. Source shells isolate real address, member-read
and member-write expressions; no global canonical layout is asserted.
All consumers deliberately share one generated module to expose accidental
co-importing of incompatible authored views. Negative cases keep missing view
evidence, incompatible imports and wrong structured field access refused.
