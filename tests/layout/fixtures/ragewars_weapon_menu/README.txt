RageWars public WeaponMenuSetup declaration-order regression.

The authored func_802181FC_de.c is exact source 28b9e9f489fa7c735764706174e67ef98fe5a80e3846bf6f304c1622666a5c7b.
WeaponMenuSetup.h is its exact local declaration. It embeds bare Menu by value.
The owner fixture retains real Slot/Menu/consumer type definitions and ordinary
typedefs in their public order, including the Menu typedef after the Menu body.
The consumer is the complete original func_80217388_de definition and its prototypes;
only unrelated common header imports and following functions were removed.

Route 23: public publication failed native gcc-2.8.1-sn64 header proof for this
unrelated consumer at NON_MATCHING=0 before Menu. The failed preprocessed file
did not survive. The reduction reconstructs it from actual source/provider bytes.
Current-main public structs_fold.fold reproduces unknown Menu; actual SN64 cc1
rejects the reduced proposed header at line 36. The tests keep native header
proof active, preprocessing and compiling its public projected consumer for all
five versions and both NON_MATCHING modes. No whole project solve or build.
