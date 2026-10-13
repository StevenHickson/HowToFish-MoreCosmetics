using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using BepInEx;
using BepInEx.Logging;
using HarmonyLib;
using UnityEngine;

namespace MoreCosmetics
{
    [BepInPlugin(Guid, "More Cosmetics", "0.2.0")]
    public class Plugin : BaseUnityPlugin
    {
        public const string Guid = "justresearch.morecosmetics";
        internal static ManualLogSource Log;

        void Awake()
        {
            Log = Logger;
            new Harmony(Guid).PatchAll();
            Log.LogInfo("More Cosmetics loaded");
        }
    }

#pragma warning disable CS0649 // filled in by JsonUtility
    /// <summary>One cosmetic as exported by blender/cosmetics.py.</summary>
    [Serializable]
    internal class CosmeticData
    {
        public string name, displayName, kind;
        public int order;   // fixed position, so list indexes never shift between versions
        public float[] positions, normals, uv0, slot, uv3;
        public int[] triangles;
        public int[] bones;        // outfits: two game bone indexes per vertex (empty = everything on the head)
        public float[] weights;    // matching weights
    }

#pragma warning restore CS0649

    internal static class Cosmetics
    {
        // Every game outfit mesh is skinned to the same 18 bones; index 1 is "Head".
        const int HeadBone = 1;

        public static readonly List<(CosmeticData data, Mesh mesh)> Loaded = new List<(CosmeticData, Mesh)>();

        public static void LoadAll(Mesh template)
        {
            if (Loaded.Count > 0) return;
            var bindposes = GetBindposes(template);
            var asm = typeof(Cosmetics).Assembly;
            foreach (var res in asm.GetManifestResourceNames().Where(r => r.StartsWith("Cosmetics.")))
            {
                try
                {
                    using var reader = new StreamReader(asm.GetManifestResourceStream(res));
                    var data = JsonUtility.FromJson<CosmeticData>(reader.ReadToEnd());
                    Loaded.Add((data, Build(data, bindposes)));
                }
                catch (Exception e) { Plugin.Log.LogError($"Couldn't load {res}: {e}"); }
            }
            Loaded.Sort((a, b) => a.data.order.CompareTo(b.data.order));
        }

        static Matrix4x4[] GetBindposes(Mesh template)
        {
            try
            {
                var bp = template ? template.bindposes : null;
                if (bp != null && bp.Length > HeadBone)
                {
                    Plugin.Log.LogInfo($"Using bind poses from '{template.name}'");
                    return bp;
                }
            }
            catch (Exception e) { Plugin.Log.LogWarning($"Couldn't read bind poses from '{template?.name}': {e.Message}"); }

            // Fallback measured with the Outfit Dumper: head bone -> mesh space at the rest pose.
            var headToMesh = new Matrix4x4(
                new Vector4(1, 0, 0, 0), new Vector4(0, 0, 1, 0), new Vector4(0, -1, 0, 0), new Vector4(0, 0, 1.09f, 1));
            var result = Enumerable.Repeat(Matrix4x4.identity, 18).ToArray();
            result[HeadBone] = headToMesh.inverse;
            Plugin.Log.LogInfo("Using measured head bind pose");
            return result;
        }

        static Mesh Build(CosmeticData d, Matrix4x4[] bindposes)
        {
            int n = d.positions.Length / 3;
            var verts = new Vector3[n];
            var normals = new Vector3[n];
            var uv0 = new Vector2[n];
            var uv1 = new Vector2[n];
            var uv2 = new Vector2[n];
            var uv3 = new Vector2[n];
            var colors = new Color[n];
            var weights = new BoneWeight[n];
            bool skinned = d.bones != null && d.bones.Length == 2 * n && d.weights != null && d.weights.Length == 2 * n;
            for (int i = 0; i < n; i++)
            {
                // Blender/OBJ space is mirrored on X relative to Unity
                verts[i] = new Vector3(-d.positions[3 * i], d.positions[3 * i + 1], d.positions[3 * i + 2]);
                normals[i] = new Vector3(-d.normals[3 * i], d.normals[3 * i + 1], d.normals[3 * i + 2]);
                uv0[i] = new Vector2(d.uv0[2 * i], d.uv0[2 * i + 1]);
                uv1[i] = new Vector2(0.01f, 0.01f);
                uv2[i] = new Vector2(d.slot[i], 0f);       // x picks skin / colour 1-3 / fixed palette colour
                uv3[i] = new Vector2(d.uv3[2 * i], d.uv3[2 * i + 1]);
                colors[i] = Color.white;
                weights[i] = skinned
                    ? new BoneWeight { boneIndex0 = d.bones[2 * i], weight0 = d.weights[2 * i],
                                       boneIndex1 = d.bones[2 * i + 1], weight1 = d.weights[2 * i + 1] }
                    : new BoneWeight { boneIndex0 = HeadBone, weight0 = 1f };
            }
            var tris = new int[d.triangles.Length];
            for (int t = 0; t < tris.Length; t += 3)
            {
                // mirroring flips the winding, so swap two corners back
                tris[t] = d.triangles[t];
                tris[t + 1] = d.triangles[t + 2];
                tris[t + 2] = d.triangles[t + 1];
            }

            var mesh = new Mesh { name = "MoreCosmetics_" + d.name };
            if (n > 65000) mesh.indexFormat = UnityEngine.Rendering.IndexFormat.UInt32;
            mesh.vertices = verts;
            mesh.normals = normals;
            mesh.SetUVs(0, uv0);
            mesh.SetUVs(1, uv1);
            mesh.SetUVs(2, uv2);
            mesh.SetUVs(3, uv3);
            mesh.colors = colors;
            mesh.triangles = tris;
            mesh.boneWeights = weights;
            mesh.bindposes = bindposes;
            mesh.RecalculateBounds();
            mesh.RecalculateTangents();
            Plugin.Log.LogInfo($"Built {d.kind} '{d.displayName}' ({tris.Length / 3} triangles)");
            return mesh;
        }
    }

    // Add our meshes to the end of the game's outfit lists. The chosen outfit is synced and saved as its
    // position in these lists, so everyone in a lobby needs the same version of the mod.
    [HarmonyPatch(typeof(SkinManager), nameof(SkinManager.Awake))]
    static class RegisterPatch
    {
        static void Postfix()
        {
            var template = SkinManager._allHats.Select(h => h?.Mesh).FirstOrDefault(m => m);
            Cosmetics.LoadAll(template);

            foreach (var (data, mesh) in Cosmetics.Loaded)
            {
                var list = data.kind switch
                {
                    "hat" => SkinManager._allHats,
                    "accessory" => SkinManager._allAccessories,
                    "outfit" => SkinManager._allOutfits,
                    _ => null,
                };
                if (list == null) { Plugin.Log.LogWarning($"Unknown cosmetic kind '{data.kind}' for {data.name}"); continue; }
                if (list.Any(i => i?.Mesh == mesh)) continue;
                list.Add(new OutfitMeshInfo(mesh, true));
                Plugin.Log.LogInfo($"Added {data.kind} '{data.displayName}' at index {list.Count - 1}");
            }

            // "Seen" flags drive the new-item dots; grow them so our items get one too
            SkinManager.SeenHats = Grow(SkinManager.SeenHats, SkinManager._allHats.Count);
            SkinManager.SeenOutfits = Grow(SkinManager.SeenOutfits, SkinManager._allOutfits.Count);
            SkinManager.SeenAccesories = Grow(SkinManager.SeenAccesories, SkinManager._allAccessories.Count);
        }

        static bool[] Grow(bool[] a, int size)
        {
            if (a == null) return new bool[size];
            if (a.Length < size) Array.Resize(ref a, size);
            return a;
        }
    }

    // One-time sanity check that bone 1 really is the head on the renderers we attach to
    [HarmonyPatch(typeof(LocalSkin), nameof(LocalSkin.EnablePreview))]
    static class BoneCheckPatch
    {
        static bool _checked;

        // EnablePreview is static, so read the singleton rather than __instance
        static void Postfix()
        {
            var ls = LocalSkin._instance;
            if (_checked || !ls || !ls._hatRenderer) return;
            _checked = true;
            var bones = ls._hatRenderer.bones;
            string name = bones.Length > 1 && bones[1] ? bones[1].name : "missing";
            if (name == "Head") Plugin.Log.LogInfo("Hat renderer bone 1 is Head, as expected");
            else Plugin.Log.LogWarning($"Hat renderer bone 1 is '{name}', not Head - custom hats may sit in the wrong place");
        }
    }
}
