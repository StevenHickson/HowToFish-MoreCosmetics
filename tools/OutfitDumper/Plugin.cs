using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Text;
using BepInEx;
using UnityEngine;

namespace OutfitDumper
{
    /// <summary>
    /// Developer tool: exports the game's hats, accessories and outfits (meshes, UVs, bone weights,
    /// renderer bones and shader values) so new cosmetics can be modelled to fit. Press F8 to dump again.
    /// Output: BepInEx/OutfitDump in the profile folder.
    /// </summary>
    [BepInPlugin("justresearch.outfitdumper", "Outfit Dumper (dev tool)", "0.1.0")]
    public class Plugin : BaseUnityPlugin
    {
        string _dir;
        bool _dumpedLists, _dumpedRenderers;
        float _nextCheck;
        static readonly CultureInfo Inv = CultureInfo.InvariantCulture;

        void Awake()
        {
            _dir = Path.Combine(Paths.BepInExRootPath, "OutfitDump");
            Logger.LogInfo($"Outfit Dumper ready. Output: {_dir} (F8 to dump again)");
        }

        void Update()
        {
            // The game only uses the new Input System; the old Input class would throw
            var kb = UnityEngine.InputSystem.Keyboard.current;
            if (kb != null && kb.f8Key.wasPressedThisFrame) { _dumpedLists = _dumpedRenderers = _dumpedBaked = false; _nextCheck = 0; }
            if (_dumpedLists && _dumpedRenderers || Time.unscaledTime < _nextCheck) return;
            _nextCheck = Time.unscaledTime + 2f;
            try
            {
                Directory.CreateDirectory(_dir);
                if (!_dumpedLists && SkinManager._instance) { DumpLists(); _dumpedLists = true; }
                if (!_dumpedRenderers && DumpRenderers()) _dumpedRenderers = true;
            }
            catch (Exception e) { Logger.LogError(e); }
        }

        // ------------------------------------------------------------ the three outfit lists

        void DumpLists()
        {
            var sb = new StringBuilder();
            DumpList(sb, "hat", SkinManager._allHats);
            DumpList(sb, "accessory", SkinManager._allAccessories);
            DumpList(sb, "outfit", SkinManager._allOutfits);
            sb.AppendLine("== Clothes sets");
            foreach (var c in SkinManager._instance._allClothes)
                if (c) sb.AppendLine($"  {c.name}: hat={N(c.HatMesh)} accessory={N(c.AccessoryMesh)} outfit={N(c.OutfitMesh)} defaultUnlocked={c.DefaultUnlocked}");
            File.WriteAllText(Path.Combine(_dir, "lists.txt"), sb.ToString());
            Logger.LogInfo($"Dumped outfit lists ({SkinManager._allHats.Count} hats, {SkinManager._allAccessories.Count} accessories, {SkinManager._allOutfits.Count} outfits)");
        }

        void DumpList(StringBuilder sb, string kind, List<OutfitMeshInfo> list)
        {
            sb.AppendLine($"== {kind} list ({list.Count})");
            for (int i = 0; i < list.Count; i++)
            {
                var mesh = list[i]?.Mesh;
                sb.AppendLine($"[{i}] {N(mesh)} unlocked={list[i]?.Unlocked}");
                if (mesh) DescribeMesh(sb, mesh, $"{kind}_{i:00}_{Safe(mesh.name)}");
            }
        }

        void DescribeMesh(StringBuilder sb, Mesh mesh, string file)
        {
            sb.AppendLine($"    readable={mesh.isReadable} verts={mesh.vertexCount} submeshes={mesh.subMeshCount} bounds={mesh.bounds} bindposes={mesh.bindposeCount}");
            if (!mesh.isReadable) { sb.AppendLine("    (not CPU readable - only metadata)"); return; }
            try
            {
                var verts = mesh.vertices;
                var normals = mesh.normals;
                var colors = mesh.colors;
                var uvs = new List<Vector4>[4];
                for (int ch = 0; ch < 4; ch++)
                {
                    var l = new List<Vector4>();
                    mesh.GetUVs(ch, l);
                    uvs[ch] = l.Count == verts.Length ? l : null;
                    if (uvs[ch] != null) sb.AppendLine($"    uv{ch}: {Distinct(l)}");
                }
                if (colors.Length == verts.Length)
                    sb.AppendLine("    colors: " + string.Join(" ", colors.Select(c => $"({c.r:0.##},{c.g:0.##},{c.b:0.##},{c.a:0.##})").GroupBy(s => s).OrderByDescending(g => g.Count()).Take(12).Select(g => $"{g.Key}x{g.Count()}")));

                var perVertex = mesh.GetBonesPerVertex();
                var weights = mesh.GetAllBoneWeights();
                if (weights.Length > 0)
                {
                    var used = weights.GroupBy(w => w.boneIndex).Select(g => $"{g.Key}:{g.Sum(w => w.weight):0.#}").ToArray();
                    sb.AppendLine($"    bone weight totals by bone index: {string.Join(" ", used)}");
                }
                var bp = mesh.bindposes;
                for (int b = 0; b < bp.Length; b++)
                {
                    var inv = bp[b].inverse; // bone's bind position in mesh space
                    sb.AppendLine($"    bindpose[{b}] bonePos={V(inv.GetColumn(3))} rot={inv.rotation.eulerAngles}");
                }

                WriteObj(Path.Combine(_dir, file + ".obj"), mesh.name, verts, normals, uvs[0], mesh);

                // Full per-vertex data for working out how colour slots are encoded
                var csv = new StringBuilder("x,y,z,u0,v0,u1,v1,u2,v2,r,g,b,a,bone0,w0,bone1,w1\n");
                int wi = 0;
                for (int v = 0; v < verts.Length; v++)
                {
                    var p = verts[v];
                    csv.Append(F(p.x)).Append(',').Append(F(p.y)).Append(',').Append(F(p.z));
                    for (int ch = 0; ch < 3; ch++)
                        csv.Append(',').Append(uvs[ch] != null ? F(uvs[ch][v].x) : "").Append(',').Append(uvs[ch] != null ? F(uvs[ch][v].y) : "");
                    if (colors.Length == verts.Length) csv.Append($",{F(colors[v].r)},{F(colors[v].g)},{F(colors[v].b)},{F(colors[v].a)}");
                    else csv.Append(",,,,");
                    int n = perVertex.Length > v ? perVertex[v] : 0;
                    for (int k = 0; k < 2; k++)
                    {
                        if (k < n) csv.Append(',').Append(weights[wi + k].boneIndex).Append(',').Append(F(weights[wi + k].weight));
                        else csv.Append(",,");
                    }
                    wi += n;
                    csv.Append('\n');
                }
                File.WriteAllText(Path.Combine(_dir, file + ".csv"), csv.ToString());
            }
            catch (Exception e) { sb.AppendLine("    error reading mesh: " + e.Message); }
        }

        // ------------------------------------------------------------ live renderers: bones, shader, posed meshes

        bool DumpRenderers()
        {
            var targets = new List<(string, Component)>();
            if (LocalSkin._instance) targets.Add(("LocalSkin", LocalSkin._instance));
            foreach (var ps in UnityEngine.Object.FindObjectsByType<PlayerSkin>()) targets.Add(("PlayerSkin_" + ps.name, ps));
            if (targets.Count == 0) return false;

            var sb = new StringBuilder();
            bool sawPlayer = false;
            foreach (var (label, comp) in targets)
            {
                SkinnedMeshRenderer hat, acc, outfit, body;
                if (comp is LocalSkin ls) { hat = ls._hatRenderer; acc = ls._accessoryRenderer; outfit = ls._outfitRenderer; body = ls._bodyRenderer; }
                else { var ps = (PlayerSkin)comp; hat = ps._hatRenderer; acc = ps._accessoryRenderer; outfit = ps._outfitRenderer; body = ps._bodyRenderer; sawPlayer = true; }

                sb.AppendLine($"######## {label}");
                Transform head = FindHead(hat);
                sb.AppendLine($"head bone guess: {(head ? head.name : "none")} at {(head ? V(head.position) : "-")}");
                foreach (var (part, r) in new[] { ("hat", hat), ("accessory", acc), ("outfit", outfit), ("body", body) })
                {
                    if (!r) { sb.AppendLine($"== {part}: none"); continue; }
                    sb.AppendLine($"== {part}: renderer '{r.name}' mesh={N(r.sharedMesh)} rootBone={(r.rootBone ? r.rootBone.name : "none")} quality={r.quality}");
                    sb.AppendLine($"   transform pos={V(r.transform.position)} rot={r.transform.rotation.eulerAngles} scale={V(r.transform.lossyScale)}");
                    var bones = r.bones;
                    for (int b = 0; b < bones.Length; b++)
                        sb.AppendLine($"   bone[{b}] {(bones[b] ? bones[b].name : "null")} pos={(bones[b] ? V(bones[b].position) : "-")}");
                    DescribeMaterial(sb, r.sharedMaterial);

                    // Posed copy in head space, so all parts line up in Blender with the head at the origin
                    if (r.sharedMesh)
                    {
                        var baked = new Mesh();
                        r.BakeMesh(baked, true);
                        var toWorld = Matrix4x4.TRS(r.transform.position, r.transform.rotation, Vector3.one);
                        var toHead = head ? head.worldToLocalMatrix * toWorld : toWorld;
                        var v = baked.vertices.Select(p => toHead.MultiplyPoint3x4(p)).ToArray();
                        var nrm = baked.normals.Select(p => toHead.MultiplyVector(p)).ToArray();
                        var uv = new List<Vector4>(); baked.GetUVs(0, uv);
                        WriteObj(Path.Combine(_dir, $"posed_{Safe(label)}_{part}.obj"), part, v, nrm, uv.Count == v.Length ? uv : null, baked);
                    }
                }
            }
            File.WriteAllText(Path.Combine(_dir, "renderers.txt"), sb.ToString());
            Logger.LogInfo($"Dumped {targets.Count} skin renderer sets");

            if (LocalSkin._instance && SkinManager._instance) DumpAllBaked(LocalSkin._instance);
            return sawPlayer; // keep checking until we've also seen a player in the world
        }

        bool _dumpedBaked;

        // The game's meshes aren't CPU-readable, but a baked (posed) copy is. Put each one on the preview
        // character in turn and export every vertex channel, in mesh space and in head space.
        void DumpAllBaked(LocalSkin ls)
        {
            if (_dumpedBaked) return;
            _dumpedBaked = true;
            var head = FindHead(ls._hatRenderer);
            var sb = new StringBuilder();
            foreach (var (kind, list, r) in new[] {
                ("hat", SkinManager._allHats, ls._hatRenderer),
                ("accessory", SkinManager._allAccessories, ls._accessoryRenderer),
                ("outfit", SkinManager._allOutfits, ls._outfitRenderer) })
            {
                if (!r) continue;
                var original = r.sharedMesh;
                for (int i = 0; i < list.Count; i++)
                {
                    var mesh = list[i]?.Mesh;
                    if (!mesh) continue;
                    r.sharedMesh = mesh;
                    var baked = new Mesh { name = mesh.name };
                    r.BakeMesh(baked, true);
                    string file = $"baked_{kind}_{i:00}_{Safe(mesh.name)}";
                    sb.AppendLine($"[{kind} {i}] {mesh.name}");
                    DescribeMesh(sb, baked, file);

                    var toWorld = Matrix4x4.TRS(r.transform.position, r.transform.rotation, Vector3.one);
                    var toHead = head ? head.worldToLocalMatrix * toWorld : toWorld;
                    var v = baked.vertices.Select(p => toHead.MultiplyPoint3x4(p)).ToArray();
                    var nrm = baked.normals.Select(p => toHead.MultiplyVector(p)).ToArray();
                    var uv = new List<Vector4>(); baked.GetUVs(0, uv);
                    WriteObj(Path.Combine(_dir, file + "_headspace.obj"), mesh.name, v, nrm, uv.Count == v.Length ? uv : null, baked);
                    Destroy(baked);
                }
                r.sharedMesh = original;
            }
            if (head)
            {
                sb.AppendLine($"head bone lossyScale={V(head.lossyScale)} rotation={head.rotation.eulerAngles}");
                var hatR = ls._hatRenderer;
                sb.AppendLine($"hat renderer worldToLocal * head.localToWorld = \n{(hatR.transform.worldToLocalMatrix * head.localToWorldMatrix)}");
            }
            File.WriteAllText(Path.Combine(_dir, "baked.txt"), sb.ToString());
            Logger.LogInfo("Dumped baked copies of every hat, accessory and outfit");

            try { DumpRestPose(ls); }
            catch (Exception e) { Logger.LogError("Rest pose dump failed: " + e); }
        }

        // Skeleton and body in the bind ("rest") pose, for building outfits in Blender.
        void DumpRestPose(LocalSkin ls)
        {
            var r = ls._outfitRenderer;
            var template = SkinManager._allHats.Select(h => h?.Mesh).FirstOrDefault(m => m);
            var bindposes = template.bindposes;
            var bones = r.bones;
            var sb = new StringBuilder("# index name parent | rest matrix (mesh space) rows\n");

            // Remember the animated pose, then put every bone at its bind pose
            var saved = bones.Select(b => (b.localPosition, b.localRotation)).ToArray();
            var anim = ls.GetComponentsInChildren<Animator>(true);
            foreach (var a in anim) a.enabled = false;
            var meshToWorld = r.transform.localToWorldMatrix;
            for (int i = 0; i < bones.Length; i++)
            {
                var rest = bindposes[i].inverse;                      // bone -> mesh space
                var world = meshToWorld * rest;
                bones[i].SetPositionAndRotation(world.GetColumn(3), world.rotation);
                sb.AppendLine($"{i} {bones[i].name} {(bones[i].parent ? bones[i].parent.name : "-")} | " +
                              string.Join(" ", Enumerable.Range(0, 4).Select(row => string.Join(",", Enumerable.Range(0, 4).Select(c => F(rest[row, c]))))));
            }
            File.WriteAllText(Path.Combine(_dir, "rest_bones.txt"), sb.ToString());

            void BakeTo(SkinnedMeshRenderer smr, Mesh mesh, string file)
            {
                if (!smr || !mesh) return;
                var original = smr.sharedMesh;
                smr.sharedMesh = mesh;
                var baked = new Mesh();
                smr.BakeMesh(baked, true);
                var uv = new List<Vector4>(); baked.GetUVs(0, uv);
                WriteObj(Path.Combine(_dir, file), mesh.name, baked.vertices, baked.normals, uv.Count == baked.vertexCount ? uv : null, baked);
                Destroy(baked);
                smr.sharedMesh = original;
            }

            for (int i = 0; i < SkinManager._allOutfits.Count; i++)
                BakeTo(r, SkinManager._allOutfits[i]?.Mesh, $"rest_outfit_{i:00}_{Safe(SkinManager._allOutfits[i]?.Mesh?.name)}.obj");
            BakeTo(ls._bodyRenderer, ls._bodyRenderer ? ls._bodyRenderer.sharedMesh : null, "rest_head.obj");
            BakeTo(ls._leftHand, ls._leftHand ? ls._leftHand.sharedMesh : null, "rest_hand_L.obj");
            BakeTo(ls._rightHand, ls._rightHand ? ls._rightHand.sharedMesh : null, "rest_hand_R.obj");

            // Put the animated pose back
            for (int i = 0; i < bones.Length; i++) bones[i].SetLocalPositionAndRotation(saved[i].localPosition, saved[i].localRotation);
            foreach (var a in anim) a.enabled = true;
            Logger.LogInfo("Dumped rest-pose skeleton and bodies");
        }

        static Transform FindHead(SkinnedMeshRenderer hat)
        {
            if (!hat) return null;
            var named = hat.bones.FirstOrDefault(b => b && b.name.ToLowerInvariant().Contains("head"));
            if (named) return named;
            if (hat.sharedMesh && hat.sharedMesh.isReadable)
            {
                var w = hat.sharedMesh.GetAllBoneWeights();
                if (w.Length > 0)
                {
                    int idx = w.GroupBy(x => x.boneIndex).OrderByDescending(g => g.Sum(x => x.weight)).First().Key;
                    if (idx < hat.bones.Length) return hat.bones[idx];
                }
            }
            return hat.rootBone;
        }

        void DescribeMaterial(StringBuilder sb, Material m)
        {
            if (!m) { sb.AppendLine("   material: none"); return; }
            var sh = m.shader;
            sb.AppendLine($"   material '{m.name}' shader '{sh.name}' keywords=[{string.Join(",", m.shaderKeywords)}]");
            for (int i = 0; i < sh.GetPropertyCount(); i++)
            {
                string n = sh.GetPropertyName(i);
                var t = sh.GetPropertyType(i);
                string val = t switch
                {
                    UnityEngine.Rendering.ShaderPropertyType.Color => m.GetColor(n).ToString(),
                    UnityEngine.Rendering.ShaderPropertyType.Vector => m.GetVector(n).ToString(),
                    UnityEngine.Rendering.ShaderPropertyType.Float or UnityEngine.Rendering.ShaderPropertyType.Range => m.GetFloat(n).ToString(Inv),
                    UnityEngine.Rendering.ShaderPropertyType.Texture => DumpTexture(m.GetTexture(n)),
                    _ => m.GetInt(n).ToString(),
                };
                sb.AppendLine($"     {n} ({t}) = {val}");
            }
        }

        readonly HashSet<Texture> _savedTextures = new HashSet<Texture>();

        string DumpTexture(Texture tex)
        {
            if (!tex) return "none";
            string file = "tex_" + Safe(tex.name) + ".png";
            if (_savedTextures.Add(tex) && tex.width <= 4096)
            {
                try
                {
                    var rt = RenderTexture.GetTemporary(tex.width, tex.height, 0, RenderTextureFormat.ARGB32, RenderTextureReadWrite.sRGB);
                    Graphics.Blit(tex, rt);
                    var prev = RenderTexture.active; RenderTexture.active = rt;
                    var t2 = new Texture2D(tex.width, tex.height, TextureFormat.RGBA32, false, false);
                    t2.ReadPixels(new Rect(0, 0, tex.width, tex.height), 0, 0);
                    t2.Apply();
                    RenderTexture.active = prev; RenderTexture.ReleaseTemporary(rt);
                    File.WriteAllBytes(Path.Combine(_dir, file), t2.EncodeToPNG());
                    Destroy(t2);
                }
                catch (Exception e) { return $"'{tex.name}' {tex.width}x{tex.height} (save failed: {e.Message})"; }
            }
            return $"'{tex.name}' {tex.width}x{tex.height} -> {file}";
        }

        // ------------------------------------------------------------ helpers

        static void WriteObj(string path, string name, Vector3[] v, Vector3[] n, List<Vector4> uv, Mesh topology)
        {
            var sb = new StringBuilder($"# {name}\no {Safe(name)}\n");
            // Unity is left-handed; flip X so the model isn't mirrored in Blender
            foreach (var p in v) sb.Append("v ").Append(F(-p.x)).Append(' ').Append(F(p.y)).Append(' ').Append(F(p.z)).Append('\n');
            if (uv != null) foreach (var t in uv) sb.Append("vt ").Append(F(t.x)).Append(' ').Append(F(t.y)).Append('\n');
            bool hasN = n != null && n.Length == v.Length;
            if (hasN) foreach (var p in n) sb.Append("vn ").Append(F(-p.x)).Append(' ').Append(F(p.y)).Append(' ').Append(F(p.z)).Append('\n');
            for (int s = 0; s < topology.subMeshCount; s++)
            {
                sb.Append("g submesh").Append(s).Append('\n');
                var tris = topology.GetTriangles(s);
                for (int i = 0; i < tris.Length; i += 3)
                {
                    sb.Append('f');
                    // reversed winding to match the X flip
                    foreach (int idx in new[] { tris[i], tris[i + 2], tris[i + 1] })
                    {
                        int k = idx + 1;
                        sb.Append(' ').Append(k);
                        if (uv != null || hasN) sb.Append('/').Append(uv != null ? k.ToString() : "");
                        if (hasN) sb.Append('/').Append(k);
                    }
                    sb.Append('\n');
                }
            }
            File.WriteAllText(path, sb.ToString());
        }

        static string Distinct(List<Vector4> l) =>
            $"range x[{l.Min(p => p.x):0.###},{l.Max(p => p.x):0.###}] y[{l.Min(p => p.y):0.###},{l.Max(p => p.y):0.###}] " +
            "common: " + string.Join(" ", l.Select(p => $"({p.x:0.##},{p.y:0.##})").GroupBy(s => s).OrderByDescending(g => g.Count()).Take(10).Select(g => $"{g.Key}x{g.Count()}"));

        static string N(UnityEngine.Object o) => o ? o.name : "none";
        static string F(float f) => f.ToString("0.#####", Inv);
        static string V(Vector3 p) => $"({F(p.x)},{F(p.y)},{F(p.z)})";
        static string Safe(string s) => new string((s ?? "null").Select(c => char.IsLetterOrDigit(c) || c == '_' || c == '-' ? c : '_').ToArray());
    }
}
