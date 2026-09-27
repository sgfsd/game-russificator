// Russificator.Unity — плагин BepInEx 5 (Mono), дополнение к XUnity.AutoTranslator.
//
// 1. Живой перевод без «сначала открой программу». При старте игры плагин читает
//    BepInEx/config/Russificator.cfg (его пишет русификатор) и, если сервер перевода
//    ещё не слушает порт, запускает русификатор в режиме --serve для этого процесса
//    игры. Так перевод на лету работает, как бы игру ни запустили: Steam, exe, ярлык.
//
// 2. TMP_Text.SetText(string). В TextMeshPro 1.x этот метод пишет текст мимо свойства
//    text, а XUnity перехватывает свойство и SetText(string, bool) — такой текст
//    оставался английским. Плагин направляет SetText(string) через свойство text.
//
// 3. Русский текст, который не помещается. После перевода плагин проверяет элемент
//    (TextMeshPro и UGUI Text): если русский текст вылезает за рамку или рвёт слово,
//    а английский помещался, включается автоподбор размера — от исходного вниз до 60%.
//    Пока текст помещается, ничего не меняется. Работает для любых элементов, в том
//    числе созданных игрой по ходу дела.
//
// 4. Надпись о программе (только в русификаторах «для друзей», параметр credit в
//    Russificator.cfg): полупрозрачная строка в правом нижнем углу первые секунды игры.
//
// Типы TextMeshPro и UGUI берутся через отражение; из UnityEngine напрямую — только то, что
// есть во всех версиях 5–6 (IMGUI для надписи). Плагин ссылается на mscorlib 2.0, System,
// BepInEx, 0Harmony и UnityEngine (в новых Unity — фасад, перенаправляющий в модули).
//
// Сборка: python tools/build_unity_plugins.py  (mcs из Mono или csc из .NET Framework 4;
// UnityEngine для компиляции — заглушка tools/unity_stubs/UnityEngine.cs).

using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Net.Sockets;
using System.Reflection;
using System.Text;
using System.Threading;
using BepInEx;
using HarmonyLib;
using UnityEngine;

[BepInPlugin("russificator.unity", "Russificator", "2.1.0")]
public class RussificatorUnity : BaseUnityPlugin
{
    const BindingFlags Inst = BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic;
    const float MinScale = 0.6f;

    static bool _fitEnabled = true;

    // надпись о программе (русификатор «для друзей»)
    const float CreditSeconds = 14f;
    static string _credit;
    float _creditStart = -1f;
    GUIStyle _creditStyle, _creditShadow;

    // ---------- состояние элементов текста ----------

    class State
    {
        public string Original;     // последний английский текст, который выставила игра
        public int CheckedLength;   // длина русского текста, для которой проверка уже была
        public int Retries;         // ожидание разметки (размер рамки ещё 0)
        public bool Fitted;         // автоподбор уже включён
    }

    static readonly Dictionary<object, State> _states = new Dictionary<object, State>();
    static readonly List<object> _queue = new List<object>();
    static readonly Dictionary<object, bool> _queued = new Dictionary<object, bool>();

    // ---------- отражение: TextMeshPro ----------

    static Type _tmp;
    static FieldInfo _tmpText;
    static PropertyInfo _tmpTextProp, _tmpRect, _tmpAuto, _tmpSize, _tmpSizeMin, _tmpSizeMax, _tmpWrap, _tmpWrapMode;
    static PropertyInfo _tmpPrefW, _tmpPrefH, _tmpTextInfo;
    static MethodInfo _tmpForceUpdate;
    static object[] _tmpForceArgs;
    static FieldInfo _tiLineCount, _tiLineInfo, _tiCharInfo, _liFirst, _liLast, _ciChar;

    // ---------- отражение: UGUI Text ----------

    static Type _ugui;
    static FieldInfo _uguiText;
    static PropertyInfo _uguiRect, _uguiBestFit, _uguiSize, _uguiMin, _uguiMax, _uguiHOverflow, _uguiGen, _uguiPpu;
    static MethodInfo _uguiSettings, _genPrefW, _genPrefH;
    static Type _vector2;

    // ---------- общее ----------

    static PropertyInfo _rectProp, _rectW, _rectH;
    static FieldInfo _v2x, _v2y;

    [ThreadStatic]
    static bool _inside;

    void Awake()
    {
        Dictionary<string, string> cfg = ReadConfig();
        string v;
        if (cfg.TryGetValue("fit", out v) && v.Trim().ToLowerInvariant() == "false") _fitEnabled = false;
        if (cfg.TryGetValue("credit", out v) && v.Trim().Length > 0) _credit = v.Trim();
        try { StartLiveServer(cfg); }
        catch (Exception ex) { Logger.LogWarning("Live translation autostart failed: " + ex.Message); }

        Hook();
        AppDomain.CurrentDomain.AssemblyLoad += OnAssemblyLoad;
    }

    void OnAssemblyLoad(object sender, AssemblyLoadEventArgs e)
    {
        if (_tmp != null && _ugui != null) return;
        try { Hook(); } catch { }
    }

    // ======================= 1. живой перевод =======================

    static Dictionary<string, string> ReadConfig()
    {
        Dictionary<string, string> cfg = new Dictionary<string, string>();
        try
        {
            string path = Path.Combine(Paths.ConfigPath, "Russificator.cfg");
            if (!File.Exists(path)) return cfg;
            foreach (string raw in File.ReadAllLines(path, Encoding.UTF8))
            {
                string line = raw.Trim();
                if (line.Length == 0 || line.StartsWith("#")) continue;
                int eq = line.IndexOf('=');
                if (eq > 0) cfg[line.Substring(0, eq).Trim()] = line.Substring(eq + 1).Trim();
            }
        }
        catch { }
        return cfg;
    }

    void StartLiveServer(Dictionary<string, string> cfg)
    {
        string exe, args, workdir, value;
        if (!cfg.TryGetValue("server", out exe) || exe.Length == 0) return;
        if (cfg.TryGetValue("autostart", out value) && value.ToLowerInvariant() == "false") return;
        int port = 47631;
        if (cfg.TryGetValue("port", out value)) int.TryParse(value, out port);
        if (PortOpen(port, 300))
        {
            Logger.LogInfo("Live translation is already running");
            return;
        }
        if (!File.Exists(exe))
        {
            Logger.LogWarning("Russificator not found (moved or deleted?): " + exe);
            return;
        }
        cfg.TryGetValue("args", out args);
        ProcessStartInfo psi = new ProcessStartInfo(exe,
            (args ?? "").Replace("{pid}", Process.GetCurrentProcess().Id.ToString()));
        psi.UseShellExecute = false;
        psi.CreateNoWindow = true;
        if (cfg.TryGetValue("workdir", out workdir) && Directory.Exists(workdir)) psi.WorkingDirectory = workdir;
        Process.Start(psi);
        // ждём, пока сервер откроет порт: иначе XUnity получит отказы и после пяти подряд
        // отключит перевод до конца игры (обычно это доли секунды)
        DateTime until = DateTime.Now.AddSeconds(20);
        while (DateTime.Now < until)
        {
            if (PortOpen(port, 250))
            {
                Logger.LogInfo("Live translation started");
                return;
            }
            Thread.Sleep(150);
        }
        Logger.LogWarning("Live translation server did not start in 20 s");
    }

    static bool PortOpen(int port, int timeoutMs)
    {
        try
        {
            using (TcpClient client = new TcpClient())
            {
                IAsyncResult ar = client.BeginConnect("127.0.0.1", port, null, null);
                if (!ar.AsyncWaitHandle.WaitOne(timeoutMs, false)) return false;
                client.EndConnect(ar);
                return true;
            }
        }
        catch
        {
            return false;
        }
    }

    // ======================= 4. надпись о программе =======================

    void OnGUI()
    {
        if (_credit == null) return;
        try
        {
            float now = Time.realtimeSinceStartup;
            if (_creditStart < 0f) _creditStart = now;
            float t = now - _creditStart;
            if (t > CreditSeconds)
            {
                _credit = null;
                return;
            }
            float alpha = t > CreditSeconds - 2f ? (CreditSeconds - t) / 2f : 1f;
            if (_creditStyle == null)
            {
                int size = Math.Max(12, Screen.height / 46);
                _creditStyle = new GUIStyle();
                _creditShadow = new GUIStyle();
                foreach (GUIStyle st in new GUIStyle[] { _creditStyle, _creditShadow })
                {
                    st.fontSize = size;
                    st.alignment = TextAnchor.LowerRight;
                    st.wordWrap = false;
                }
            }
            _creditStyle.normal.textColor = new Color(1f, 1f, 1f, 0.85f * alpha);
            _creditShadow.normal.textColor = new Color(0f, 0f, 0f, 0.65f * alpha);
            float w = Screen.width, h = Screen.height, pad = Math.Max(10f, h / 70f);
            GUI.Label(new Rect(0f, 0f, w - pad + 1.5f, h - pad + 1.5f), _credit, _creditShadow);
            GUI.Label(new Rect(0f, 0f, w - pad, h - pad), _credit, _creditStyle);
        }
        catch (Exception ex)
        {
            _credit = null;
            Logger.LogDebug("credit: " + ex.Message);
        }
    }

    // ======================= перехваты =======================

    static Type FindType(string name)
    {
        foreach (Assembly a in AppDomain.CurrentDomain.GetAssemblies())
        {
            Type t = null;
            try { t = a.GetType(name, false); } catch { }
            if (t != null) return t;
        }
        return null;
    }

    void Hook()
    {
        Harmony harmony = new Harmony("russificator.unity");
        if (_tmp == null)
        {
            Type t = FindType("TMPro.TMP_Text");
            if (t != null)
            {
                _tmp = t;
                HookTmp(harmony);
            }
        }
        if (_ugui == null)
        {
            Type t = FindType("UnityEngine.UI.Text");
            if (t != null)
            {
                _ugui = t;
                HookUgui(harmony);
            }
        }
    }

    static MethodInfo Own(string name)
    {
        return typeof(RussificatorUnity).GetMethod(name, BindingFlags.Static | BindingFlags.NonPublic);
    }

    void HookTmp(Harmony harmony)
    {
        try
        {
            _tmpTextProp = _tmp.GetProperty("text");
            _tmpText = FindField(_tmp, "m_text");
            _tmpRect = _tmp.GetProperty("rectTransform");
            _tmpAuto = _tmp.GetProperty("enableAutoSizing");
            _tmpSize = _tmp.GetProperty("fontSize");
            _tmpSizeMin = _tmp.GetProperty("fontSizeMin");
            _tmpSizeMax = _tmp.GetProperty("fontSizeMax");
            _tmpWrap = _tmp.GetProperty("enableWordWrapping");
            _tmpWrapMode = _tmp.GetProperty("textWrappingMode");
            // только свойства текущего текста: GetPreferredValues(string) переписывает внутренний
            // буфер компонента и может вернуть на экран английский текст
            _tmpPrefW = _tmp.GetProperty("preferredWidth");
            _tmpPrefH = _tmp.GetProperty("preferredHeight");
            _tmpTextInfo = _tmp.GetProperty("textInfo");
            foreach (MethodInfo m in _tmp.GetMethods(BindingFlags.Instance | BindingFlags.Public))
            {
                if (m.Name != "ForceMeshUpdate") continue;
                ParameterInfo[] ps = m.GetParameters();
                bool allBool = true;
                foreach (ParameterInfo pi in ps) allBool &= pi.ParameterType == typeof(bool);
                if (allBool && (_tmpForceUpdate == null || ps.Length < _tmpForceUpdate.GetParameters().Length))
                    _tmpForceUpdate = m;
            }
            if (_tmpForceUpdate != null)
            {
                _tmpForceArgs = new object[_tmpForceUpdate.GetParameters().Length];
                for (int i = 0; i < _tmpForceArgs.Length; i++) _tmpForceArgs[i] = false;
            }
            if (_tmpTextInfo != null)
            {
                Type ti = _tmpTextInfo.PropertyType;
                _tiLineCount = ti.GetField("lineCount");
                _tiLineInfo = ti.GetField("lineInfo");
                _tiCharInfo = ti.GetField("characterInfo");
                if (_tiLineInfo != null && _tiCharInfo != null)
                {
                    _liFirst = _tiLineInfo.FieldType.GetElementType().GetField("firstCharacterIndex");
                    _liLast = _tiLineInfo.FieldType.GetElementType().GetField("lastCharacterIndex");
                    _ciChar = _tiCharInfo.FieldType.GetElementType().GetField("character");
                }
            }

            MethodInfo setText = _tmp.GetMethod("SetText", new Type[] { typeof(string) });
            if (setText != null && _tmpTextProp != null && _tmpTextProp.GetSetMethod() != null)
                harmony.Patch(setText, new HarmonyMethod(Own("SetTextPrefix")));

            MethodInfo setter = _tmpTextProp != null ? _tmpTextProp.GetSetMethod() : null;
            if (setter != null && _fitEnabled && _tmpText != null && _tmpRect != null && _tmpAuto != null
                && _tmpPrefW != null && _tmpPrefH != null && _tmpSize != null && _tmpSizeMin != null && _tmpSizeMax != null)
            {
                harmony.Patch(setter, new HarmonyMethod(Own("TextPrefix")), new HarmonyMethod(Own("TextPostfix")));
            }
            Logger.LogInfo("TextMeshPro hooks installed");
        }
        catch (Exception ex)
        {
            Logger.LogWarning("TextMeshPro hooks failed: " + ex.Message);
        }
    }

    void HookUgui(Harmony harmony)
    {
        try
        {
            if (!_fitEnabled) return;
            _uguiText = FindField(_ugui, "m_Text");
            _uguiRect = _ugui.GetProperty("rectTransform");
            _uguiBestFit = _ugui.GetProperty("resizeTextForBestFit");
            _uguiSize = _ugui.GetProperty("fontSize");
            _uguiMin = _ugui.GetProperty("resizeTextMinSize");
            _uguiMax = _ugui.GetProperty("resizeTextMaxSize");
            _uguiHOverflow = _ugui.GetProperty("horizontalOverflow");
            _uguiGen = _ugui.GetProperty("cachedTextGeneratorForLayout");
            _uguiPpu = _ugui.GetProperty("pixelsPerUnit");
            foreach (MethodInfo m in _ugui.GetMethods(BindingFlags.Instance | BindingFlags.Public))
                if (m.Name == "GetGenerationSettings" && m.GetParameters().Length == 1) _uguiSettings = m;
            if (_uguiGen != null && _uguiSettings != null)
            {
                Type gen = _uguiGen.PropertyType;
                Type settings = _uguiSettings.ReturnType;
                _genPrefW = gen.GetMethod("GetPreferredWidth", new Type[] { typeof(string), settings });
                _genPrefH = gen.GetMethod("GetPreferredHeight", new Type[] { typeof(string), settings });
                Remember(_uguiSettings.GetParameters()[0].ParameterType);
            }
            PropertyInfo text = _ugui.GetProperty("text");
            MethodInfo setter = text != null ? text.GetSetMethod() : null;
            if (setter != null && _uguiText != null && _uguiRect != null && _uguiBestFit != null
                && _genPrefW != null && _genPrefH != null)
            {
                harmony.Patch(setter, new HarmonyMethod(Own("TextPrefix")), new HarmonyMethod(Own("TextPostfix")));
                Logger.LogInfo("UGUI hooks installed");
            }
        }
        catch (Exception ex)
        {
            Logger.LogWarning("UGUI hooks failed: " + ex.Message);
        }
    }

    static FieldInfo FindField(Type t, string name)
    {
        for (Type x = t; x != null; x = x.BaseType)
        {
            FieldInfo f = x.GetField(name, Inst | BindingFlags.DeclaredOnly);
            if (f != null) return f;
        }
        return null;
    }

    static void Remember(Type vector2)
    {
        if (_vector2 != null || vector2 == null) return;
        _vector2 = vector2;
        _v2x = vector2.GetField("x");
        _v2y = vector2.GetField("y");
    }

    // ---------- 2. SetText(string) -> text ----------

    static bool SetTextPrefix(object __instance, string __0)
    {
        if (_inside) return true;
        _inside = true;
        try { _tmpTextProp.SetValue(__instance, __0, null); }
        catch { _inside = false; return true; }
        finally { _inside = false; }
        return false;
    }

    // ---------- 3. проверка, помещается ли перевод ----------

    static bool HasCyrillic(string s)
    {
        if (s == null) return false;
        for (int i = 0; i < s.Length; i++)
        {
            char c = s[i];
            if (c >= '\u0400' && c <= '\u04FF') return true;
        }
        return false;
    }

    static State StateOf(object inst)
    {
        State st;
        if (!_states.TryGetValue(inst, out st))
        {
            if (_states.Count > 4000) Prune();
            st = new State();
            _states[inst] = st;
        }
        return st;
    }

    static void Prune()
    {
        List<object> dead = new List<object>();
        foreach (object k in _states.Keys)
            if (k == null || k.Equals(null)) dead.Add(k);    // уничтоженные объекты Unity равны null
        foreach (object k in dead) _states.Remove(k);
        if (_states.Count > 4000) _states.Clear();
    }

    static void TextPrefix(object __instance, string value)
    {
        try
        {
            if (value != null && value.Length > 0 && !HasCyrillic(value)) StateOf(__instance).Original = value;
        }
        catch { }
    }

    static void TextPostfix(object __instance)
    {
        try
        {
            FieldInfo f = _tmp != null && _tmp.IsInstanceOfType(__instance) ? _tmpText : _uguiText;
            if (f == null) return;
            string s = f.GetValue(__instance) as string;
            if (!HasCyrillic(s)) return;
            State st = StateOf(__instance);
            if (st.Fitted || s.Length <= st.CheckedLength) return;
            if (!_queued.ContainsKey(__instance))
            {
                _queued[__instance] = true;
                _queue.Add(__instance);
            }
        }
        catch { }
    }

    void Update()
    {
        if (_queue.Count == 0) return;
        // проверяем в следующем кадре: к этому времени Unity уже расставит размеры рамок
        int n = Math.Min(_queue.Count, 40);
        object[] batch = _queue.GetRange(0, n).ToArray();
        _queue.RemoveRange(0, n);
        foreach (object inst in batch)
        {
            _queued.Remove(inst);
            try { Check(inst); }
            catch (Exception ex) { Logger.LogDebug("fit check failed: " + ex.Message); }
        }
    }

    void Check(object inst)
    {
        if (inst == null || inst.Equals(null)) return;
        bool tmp = _tmp != null && _tmp.IsInstanceOfType(inst);
        FieldInfo f = tmp ? _tmpText : _uguiText;
        string s = f.GetValue(inst) as string;
        if (!HasCyrillic(s)) return;
        State st = StateOf(inst);
        if (st.Fitted || s.Length <= st.CheckedLength) return;

        object rt = (tmp ? _tmpRect : _uguiRect).GetValue(inst, null);
        if (rt == null) return;
        if (_rectProp == null)
        {
            _rectProp = rt.GetType().GetProperty("rect");
            _rectW = _rectProp.PropertyType.GetProperty("width");
            _rectH = _rectProp.PropertyType.GetProperty("height");
        }
        object rect = _rectProp.GetValue(rt, null);
        float w = (float)_rectW.GetValue(rect, null);
        float h = (float)_rectH.GetValue(rect, null);
        if (w < 1f || h < 1f)
        {
            if (++st.Retries < 30 && !_queued.ContainsKey(inst))
            {
                _queued[inst] = true;
                _queue.Add(inst);      // рамка ещё не рассчитана — проверим позже
            }
            return;
        }
        st.CheckedLength = s.Length;
        if (tmp ? (bool)_tmpAuto.GetValue(inst, null) : (bool)_uguiBestFit.GetValue(inst, null)) return;
        if (tmp)
        {
            // рамка уже одной буквы — текст нарочно выходит за неё (так задумано), не трогаем
            float size = (float)_tmpSize.GetValue(inst, null);
            if (w < size * 0.75f || h < size * 0.5f) return;
            if (!TmpOverflows(inst, w, h)) return;
            FitTmp(inst);
        }
        else
        {
            if (!UguiOverflows(inst, s, w, h)) return;
            // английский текст тоже не помещался — так задумано, не трогаем
            if (st.Original != null && UguiOverflows(inst, st.Original, w, h)) return;
            FitUgui(inst);
        }
        st.Fitted = true;
        if (_fitLogged++ < 100) Logger.LogInfo("Autosize: " + NameOf(inst) + " — " + Shorten(s));
    }

    static int _fitLogged;

    static string NameOf(object inst)
    {
        try
        {
            PropertyInfo name = inst.GetType().GetProperty("name");
            return name != null ? (string)name.GetValue(inst, null) : inst.GetType().Name;
        }
        catch
        {
            return inst.GetType().Name;
        }
    }

    static string Shorten(string s)
    {
        s = s.Replace("\n", " ");
        return s.Length > 60 ? s.Substring(0, 60) + "…" : s;
    }

    static List<string> LongestWords(string s)
    {
        StringBuilder plain = new StringBuilder(s.Length);
        bool tag = false;
        foreach (char c in s)
        {
            if (c == '<') tag = true;
            else if (c == '>' && tag) { tag = false; plain.Append(' '); }
            else if (!tag) plain.Append(c);
        }
        List<string> words = new List<string>(plain.ToString().Split(new char[] { ' ', '\n', '\r', '\t' },
                                                                     StringSplitOptions.RemoveEmptyEntries));
        words.Sort(delegate (string a, string b) { return b.Length.CompareTo(a.Length); });
        if (words.Count > 3) words.RemoveRange(3, words.Count - 3);
        return words;
    }

    static bool TmpWraps(object inst)
    {
        if (_tmpWrap != null) return (bool)_tmpWrap.GetValue(inst, null);
        if (_tmpWrapMode != null)
        {
            int mode = Convert.ToInt32(_tmpWrapMode.GetValue(inst, null));
            return mode == 1 || mode == 2;     // Normal, PreserveWhitespace
        }
        return true;
    }

    static bool TmpOverflows(object inst, float w, float h)
    {
        if (!TmpWraps(inst)) return (float)_tmpPrefW.GetValue(inst, null) > w + 1.5f;
        if ((float)_tmpPrefH.GetValue(inst, null) > h + 1.5f) return true;
        return TmpBrokenWord(inst);
    }

    // Перенос строки посреди слова: слово длиннее строки TextMeshPro режет по буквам.
    static bool TmpBrokenWord(object inst)
    {
        if (_tmpTextInfo == null || _tiLineCount == null || _liFirst == null || _liLast == null || _ciChar == null)
            return false;
        if (_tmpForceUpdate != null) _tmpForceUpdate.Invoke(inst, _tmpForceArgs);
        object info = _tmpTextInfo.GetValue(inst, null);
        if (info == null) return false;
        int lines = (int)_tiLineCount.GetValue(info);
        Array li = _tiLineInfo.GetValue(info) as Array;
        Array ci = _tiCharInfo.GetValue(info) as Array;
        if (li == null || ci == null) return false;
        for (int i = 0; i + 1 < lines && i + 1 < li.Length; i++)
        {
            int last = (int)_liLast.GetValue(li.GetValue(i));
            int first = (int)_liFirst.GetValue(li.GetValue(i + 1));
            if (last < 0 || first <= 0 || last >= ci.Length || first >= ci.Length || first != last + 1) continue;
            char a = (char)_ciChar.GetValue(ci.GetValue(last));
            char b = (char)_ciChar.GetValue(ci.GetValue(first));
            if (char.IsLetterOrDigit(a) && char.IsLetterOrDigit(b)) return true;
        }
        return false;
    }

    static bool UguiOverflows(object inst, string s, float w, float h)
    {
        object gen = _uguiGen.GetValue(inst, null);
        float ppu = (float)_uguiPpu.GetValue(inst, null);
        if (gen == null || ppu <= 0f) return false;
        object extents = Activator.CreateInstance(_vector2, new object[] { w, 0f });
        object settings = _uguiSettings.Invoke(inst, new object[] { extents });
        bool wrap = Convert.ToInt32(_uguiHOverflow.GetValue(inst, null)) == 0;
        if (wrap)
        {
            float ph = (float)_genPrefH.Invoke(gen, new object[] { s, settings }) / ppu;
            if (ph > h + 1.5f) return true;
            foreach (string word in LongestWords(s))
            {
                float ww = (float)_genPrefW.Invoke(gen, new object[] { word, settings }) / ppu;
                if (ww > w + 1f) return true;
            }
            return false;
        }
        float pw = (float)_genPrefW.Invoke(gen, new object[] { s, settings }) / ppu;
        return pw > w + 1.5f;
    }

    static void FitTmp(object inst)
    {
        float size = (float)_tmpSize.GetValue(inst, null);
        if (size <= 1f) return;
        _tmpSizeMax.SetValue(inst, size, null);
        _tmpSizeMin.SetValue(inst, (float)Math.Max(1.0, Math.Floor(size * MinScale)), null);
        _tmpAuto.SetValue(inst, true, null);
    }

    static void FitUgui(object inst)
    {
        int size = (int)_uguiSize.GetValue(inst, null);
        if (size <= 1) return;
        _uguiMax.SetValue(inst, size, null);
        _uguiMin.SetValue(inst, (int)Math.Max(1.0, Math.Floor(size * MinScale)), null);
        if (Convert.ToInt32(_uguiHOverflow.GetValue(inst, null)) != 0)   // однострочная подпись: перенос + подбор
            _uguiHOverflow.SetValue(inst, Enum.ToObject(_uguiHOverflow.PropertyType, 0), null);
        _uguiBestFit.SetValue(inst, true, null);
    }
}
