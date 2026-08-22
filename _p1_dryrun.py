import json, sys
sys.path.insert(0, "D:/HCM_ASST/hcm-v2/tools")
import quality_scorer as qs
bl = json.load(open("D:/HCM_ASST/hcm-v2/tools/models/feature_baseline.json"))
feats = bl["features"]
normal = {c: bl["mean"][c] for c in feats}
as1, d1, o1, l1 = qs._inference_defense(80.0, normal, bl)
print("NORMAL  -> ai_score=%s psi_like=%.4f max_z=%.4f level=%s" % (as1, d1, o1, l1))
extreme = dict(normal)
for c in feats:
    sd = bl["std"].get(c) or 0.0
    if sd > 1e-9:
        extreme[c] = bl["mean"][c] + 10.0*sd
        break
as2, d2, o2, l2 = qs._inference_defense(80.0, extreme, bl)
print("EXTREME -> ai_score=%s psi_like=%.4f max_z=%.4f level=%s" % (as2, d2, o2, l2))
mild = dict(normal)
for c in feats:
    sd = bl["std"].get(c) or 0.0
    if sd > 1e-9:
        mild[c] = bl["mean"][c] + 3.5*sd
        break
as3, d3, o3, l3 = qs._inference_defense(80.0, mild, bl)
print("MILD    -> ai_score=%s psi_like=%.4f max_z=%.4f level=%s" % (as3, d3, o3, l3))
