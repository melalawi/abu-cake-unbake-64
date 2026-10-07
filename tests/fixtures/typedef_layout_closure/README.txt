Extracted from committed BattleTanx consumer/provider headers at
3d96f6043dc67fe99417dfb651957b099af4f75f. Keep authored guards, publication
markers, the provider definition/typedef, and the consumer value field.
Other declarations were removed. The disposable layout index retained the
consumer while pointing QueryBox at an absent older provider home; the
consumer explicitly includes this present committed provider. No compiler
or whole-project type solve is needed to reproduce the refusal.
