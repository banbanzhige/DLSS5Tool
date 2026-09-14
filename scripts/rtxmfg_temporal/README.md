# Pinned RTXMFG temporal adapter (research)

Source: https://github.com/dashdogy/RTX40MFG-Unlock/tree/33b41835dc39c5d8ab1ef93efb2449be31139c09/source/native

Only the baseline Ada temporal kernel correction and provider policy are retained.
Output-pull, mask, prev2curr and scatter paths are omitted; baseline temporal
source/PTX/output hashes and layout checks are unchanged. Upstream's full
v1.3.3 build enables the mask-only optimization; this research subset does not
reproduce that complete build and makes no equivalence claim for it.
No kernel/model payload or game proxy is bundled. Only an exact-hash pinned
provider is admitted by the outer research probe. This is not production support.
