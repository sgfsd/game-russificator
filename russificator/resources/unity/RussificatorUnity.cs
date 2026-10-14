// Russificator.Unity — дополнение к XUnity.AutoTranslator. Один исходник, две сборки:
//   * Russificator.Unity.dll         — BepInEx 5, Mono-игры (Unity 5–6);
//   * Russificator.Unity.IL2CPP.dll  — BepInEx 6, IL2CPP-игры (символ IL2CPP).
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
// 5. TextMesh (Mono): у него нет переноса строк — игры переносят текст сами, вставляя
//    переводы строк по ширине английского. Русский текст, который пришёл одной строкой
//    (а игра перенесла бы), разбивается по той же ширине; текст, перенесённый игрой, ищется
//    в словаре и без её переносов; надписи, которые игра переставляет каждые несколько кадров
//    (мигающее «insert coin» — XUnity такие перестаёт переводить), сразу получают перевод из
//    словаря XUnity. Если перевод выключен (Alt+T) — ничего не делается.
//
// 6. Текстовые компоненты, которых XUnity не знает (ex2D exSpriteFont, Mono): текст переводится
//    через XUnity — словарь, правила, живой перевод; буквы для их растровых шрифтов дорисовывает
//    русификатор.
//
// 7. После перестройки текстуры динамического шрифта (в неё добавляются русские буквы) все TextMesh
//    с этим шрифтом, в том числе неактивные, строятся заново — иначе в них остаются старые координаты
//    букв и на экране мусор.
//
// Типы TextMeshPro и UGUI берутся через отражение (в IL2CPP — обёртки Il2CppInterop,
// поля там видны как свойства, а объект узнаётся по указателю); из UnityEngine напрямую —
// только IMGUI для надписи. Любой сбой внутри плагина выключает только его часть, игру
// он не ломает.
//
// Сборка: python tools/build_unity_plugins.py (mcs из Mono или csc; UnityEngine для
// компиляции — заглушки tools/unity_stubs, в игре — настоящие сборки игры).

using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Net.Sockets;
using System.Reflection;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using BepInEx;
using BepInEx.Logging;
using HarmonyLib;
using UnityEngine;
#if IL2CPP
using BepInEx.Unity.IL2CPP;
using Il2CppInterop.Runtime.Injection;
using Il2CppInterop.Runtime.InteropTypes;
#endif

#if IL2CPP
[BepInPlugin("russificator.unity.il2cpp", "Russificator", "2.1.0")]
public class RussificatorUnity : BasePlugin
#else
[BepInPlugin("russificator.unity", "Russificator", "2.1.0")]
public class RussificatorUnity : BaseUnityPlugin
#endif
{
    const BindingFlags Inst = BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic;
    const float MinScale = 0.6f;

    static ManualLogSource _log;
    static bool _fitEnabled = true;

    // надпись о программе (русификатор «для друзей»)
    const float CreditSeconds = 14f;
    static string _credit;
    static float _creditStart = -1f;
    static GUIStyle _creditStyle, _creditShadow;

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
    static PropertyInfo _uguiTextProp, _uguiRect, _uguiBestFit, _uguiSize, _uguiMin, _uguiMax, _uguiHOverflow;
    static PropertyInfo _uguiGen, _uguiPpu;
    static MethodInfo _uguiSettings, _genPrefW, _genPrefH;
    static Type _vector2;

    // ---------- общее ----------

    static PropertyInfo _rectProp, _rectW, _rectH;

    [ThreadStatic]
    static bool _inside;

    // ======================= запуск =======================

#if IL2CPP
    public override void Load()
    {
        _log = Log;
        Start();
        try
        {
            ClassInjector.RegisterTypeInIl2Cpp<RussificatorBehaviour>();
            AddComponent<RussificatorBehaviour>();
        }
        catch (Exception ex)
        {
            _log.LogWarning("Per-frame component failed (autosize and credit are off): " + ex.Message);
        }
    }
#else
    void Awake()
    {
        _log = Logger;
        Start();
    }

    void Update()
    {
        Tick();
    }

    void OnGUI()
    {
        if (_credit == null) return;
        try { DrawCredit(); }
        catch (Exception ex) { _credit = null; _log.LogDebug("credit: " + ex.Message); }
    }
#endif

    static void Start()
    {
        Dictionary<string, string> cfg = ReadConfig();
        string v;
        if (cfg.TryGetValue("fit", out v) && v.Trim().ToLowerInvariant() == "false") _fitEnabled = false;
        if (cfg.TryGetValue("credit", out v) && v.Trim().Length > 0) _credit = v.Trim();
        try { StartLiveServer(cfg); }
        catch (Exception ex) { _log.LogWarning("Live translation autostart failed: " + ex.Message); }

        Hook();
        AppDomain.CurrentDomain.AssemblyLoad += OnAssemblyLoad;
    }

    static void OnAssemblyLoad(object sender, AssemblyLoadEventArgs e)
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

    static void StartLiveServer(Dictionary<string, string> cfg)
    {
        string exe, args, workdir, value;
        if (!cfg.TryGetValue("server", out exe) || exe.Length == 0) return;
        if (cfg.TryGetValue("autostart", out value) && value.ToLowerInvariant() == "false") return;
        int port = 47631;
        if (cfg.TryGetValue("port", out value)) int.TryParse(value, out port);
        if (PortOpen(port, 300))
        {
            _log.LogInfo("Live translation is already running");
            return;
        }
        if (!File.Exists(exe))
        {
            _log.LogWarning("Russificator not found (moved or deleted?): " + exe);
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
        // отключит перевод до конца игры (обычно это доли секунды). Но не дольше BlockSeconds:
        // плагины старых Unity грузятся вместе с первой сценой (часто это уже главное меню), и
        // долгое ожидание здесь — это зависшая игра. Дальше ждём в фоне, только чтобы записать итог.
        if (WaitPort(port, BlockSeconds, false)) return;
        Thread waiter = new Thread(delegate ()
        {
            try { WaitPort(port, 20 - BlockSeconds, true); }
            catch { }
        });
        waiter.IsBackground = true;
        waiter.Start();
    }

    const int BlockSeconds = 3;

    static bool WaitPort(int port, int seconds, bool report)
    {
        DateTime until = DateTime.Now.AddSeconds(seconds);
        while (DateTime.Now < until)
        {
            if (PortOpen(port, 250))
            {
                _log.LogInfo("Live translation started");
                return true;
            }
            Thread.Sleep(150);
        }
        if (report) _log.LogWarning("Live translation server did not start in 20 s");
        return false;
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

    public static void DrawCredit()
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
        _creditStyle.normal.textColor = Rgba(1f, 1f, 1f, 0.85f * alpha);
        _creditShadow.normal.textColor = Rgba(0f, 0f, 0f, 0.65f * alpha);
        float w = Screen.width, h = Screen.height, pad = Math.Max(10f, h / 70f);
        GUI.Label(new Rect(0f, 0f, w - pad + 1.5f, h - pad + 1.5f), _credit, _creditShadow);
        GUI.Label(new Rect(0f, 0f, w - pad, h - pad), _credit, _creditStyle);
    }

    static Color Rgba(float r, float g, float b, float a)
    {
        Color c = new Color();
        c.r = r;
        c.g = g;
        c.b = b;
        c.a = a;
        return c;
    }

    public static bool CreditActive
    {
        get { return _credit != null; }
    }

    public static void StopCredit(Exception ex)
    {
        _credit = null;
        if (ex != null) _log.LogDebug("credit: " + ex.Message);
    }

    // ======================= перехваты =======================

    static Type FindType(params string[] names)
    {
        foreach (Assembly a in AppDomain.CurrentDomain.GetAssemblies())
        {
            foreach (string name in names)
            {
                Type t = null;
                try { t = a.GetType(name, false); } catch { }
                if (t != null) return t;
            }
        }
        return null;
    }

    static void Hook()
    {
        Harmony harmony = new Harmony("russificator.unity");
        if (_tmp == null)
        {
            Type t = FindType("TMPro.TMP_Text", "Il2CppTMPro.TMP_Text");
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
#if !IL2CPP
        if (_textMesh == null)
        {
            Type t = FindType("UnityEngine.TextMesh");
            if (t != null)
            {
                _textMesh = t;
                HookTextMesh(harmony);
                HookFontRebuild();
            }
        }
        HookForeign(harmony);
#endif
    }

    static MethodInfo Own(string name)
    {
        return typeof(RussificatorUnity).GetMethod(name, BindingFlags.Static | BindingFlags.NonPublic);
    }

    static void HookTmp(Harmony harmony)
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
                if (_tiLineInfo != null && _tiCharInfo != null && _tiLineInfo.FieldType.IsArray
                    && _tiCharInfo.FieldType.IsArray)
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
            if (setter != null && _fitEnabled && _tmpRect != null && _tmpAuto != null
                && _tmpPrefW != null && _tmpPrefH != null && _tmpSize != null && _tmpSizeMin != null && _tmpSizeMax != null)
            {
                harmony.Patch(setter, new HarmonyMethod(Own("TextPrefix")), new HarmonyMethod(Own("TextPostfix")));
            }
            _log.LogInfo("TextMeshPro hooks installed");
        }
        catch (Exception ex)
        {
            _log.LogWarning("TextMeshPro hooks failed: " + ex.Message);
        }
    }

    static void HookUgui(Harmony harmony)
    {
        try
        {
            if (!_fitEnabled) return;
            _uguiText = FindField(_ugui, "m_Text");
            _uguiTextProp = _ugui.GetProperty("text");
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
                _vector2 = _uguiSettings.GetParameters()[0].ParameterType;
            }
            MethodInfo setter = _uguiTextProp != null ? _uguiTextProp.GetSetMethod() : null;
            if (setter != null && (_uguiText != null || _uguiTextProp.GetGetMethod() != null) && _uguiRect != null
                && _uguiBestFit != null && _genPrefW != null && _genPrefH != null)
            {
                harmony.Patch(setter, new HarmonyMethod(Own("TextPrefix")), new HarmonyMethod(Own("TextPostfix")));
                _log.LogInfo("UGUI hooks installed");
            }
        }
        catch (Exception ex)
        {
            _log.LogWarning("UGUI hooks failed: " + ex.Message);
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

    // ---------- объекты Unity: ключ, текст, «жив ли» ----------

    // В IL2CPP каждый вызов может прийти с новой обёрткой одного и того же объекта —
    // состояние элемента храним по указателю на объект игры.
    static object KeyOf(object inst)
    {
#if IL2CPP
        Il2CppObjectBase o = inst as Il2CppObjectBase;
        if (o != null) return o.Pointer;
#endif
        return inst;
    }

    static bool Alive(object inst)
    {
        try
        {
            if (inst == null) return false;
#if IL2CPP
            Il2CppObjectBase o = inst as Il2CppObjectBase;
            if (o != null && o.WasCollected) return false;
#endif
            return !inst.Equals(null);        // уничтоженные объекты Unity равны null
        }
        catch
        {
            return false;
        }
    }

    // Текст элемента: поле (Mono — мимо перехватов XUnity) или свойство (IL2CPP — полей у обёрток нет).
    static string ReadText(object inst, bool tmp)
    {
        FieldInfo f = tmp ? _tmpText : _uguiText;
        if (f != null) return f.GetValue(inst) as string;
        PropertyInfo p = tmp ? _tmpTextProp : _uguiTextProp;
        return p != null ? p.GetValue(inst, null) as string : null;
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

    // ---------- 5. TextMesh: перенос по ширине оригинала, словарь XUnity для «мигающих» надписей ----------

#if !IL2CPP
    static Type _textMesh;
    static bool _textMeshOff;
    static bool _xuaLooked;
    static object _xuaTranslator, _xuaPlugin;
    static MethodInfo _xuaTry;
    static FieldInfo _xuaMode;
    static PropertyInfo _xuaModeProp;

    static void HookTextMesh(Harmony harmony)
    {
        try
        {
            PropertyInfo text = _textMesh.GetProperty("text");
            MethodInfo setter = text != null ? text.GetSetMethod() : null;
            if (setter == null) return;
            harmony.Patch(setter, new HarmonyMethod(Own("TextMeshPrefix")));
            _log.LogInfo("TextMesh hooks installed");
        }
        catch (Exception ex)
        {
            _log.LogWarning("TextMesh hooks failed: " + ex.Message);
        }
    }

    // XUnity: словарь (TryTranslate) и режим «перевод включён» (Alt+T) — через отражение
    static void FindXua()
    {
        if (_xuaLooked) return;
        _xuaLooked = true;
        try
        {
            Type api = FindType("XUnity.AutoTranslator.Plugin.Core.AutoTranslator");
            Type itf = FindType("XUnity.AutoTranslator.Plugin.Core.ITranslator");
            PropertyInfo def = api != null ? api.GetProperty("Default", BindingFlags.Static | BindingFlags.Public) : null;
            _xuaTranslator = def != null ? def.GetValue(null, null) : null;
            if (itf != null)
                _xuaTry = itf.GetMethod("TryTranslate", new Type[] { typeof(string), typeof(string).MakeByRefType() });
            Type plugin = FindType("XUnity.AutoTranslator.Plugin.Core.AutoTranslationPlugin");
            if (plugin != null)
            {
                const BindingFlags Any = BindingFlags.Static | BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic;
                PropertyInfo cur = plugin.GetProperty("Current", BindingFlags.Static | BindingFlags.Public | BindingFlags.NonPublic);
                FieldInfo curField = plugin.GetField("Current", BindingFlags.Static | BindingFlags.Public | BindingFlags.NonPublic);
                _xuaPlugin = cur != null ? cur.GetValue(null, null) : curField != null ? curField.GetValue(null) : null;
                _xuaMode = plugin.GetField("_isInTranslatedMode", Any);
                if (_xuaMode == null) _xuaModeProp = plugin.GetProperty("IsInTranslatedMode", Any);
            }
        }
        catch (Exception ex)
        {
            _log.LogDebug("XUnity API: " + ex.Message);
        }
        if (_xuaTranslator == null || _xuaTry == null) _xuaLooked = false;   // XUnity ещё не готов — спросим позже
    }

    static bool XuaTranslating()
    {
        try
        {
            if (_xuaMode != null) return (bool)_xuaMode.GetValue(_xuaMode.IsStatic ? null : _xuaPlugin);
            if (_xuaModeProp != null) return (bool)_xuaModeProp.GetValue(_xuaPlugin, null);
        }
        catch { }
        return true;
    }

    static bool TryXua(string text, out string tr)
    {
        if (TryXuaExact(text, out tr)) return true;
        // «+2 Vitality», «Level 7»: целиком строки в словаре нет — число остаётся, слово по словарю
        Match m = NumberFirst.Match(text);
        if (m.Success && TryXuaExact(m.Groups[2].Value, out tr)) { tr = m.Groups[1].Value + tr; return true; }
        m = NumberLast.Match(text);
        if (m.Success && TryXuaExact(m.Groups[1].Value, out tr)) { tr = tr + m.Groups[2].Value; return true; }
        tr = null;
        return false;
    }

    static readonly Regex NumberFirst = new Regex(@"^([-+\u2212]?\d+(?:[.,]\d+)?%?\s+)(\p{L}[\S\s]*)$");
    static readonly Regex NumberLast = new Regex(@"^(\p{L}[\S\s]*?)(:?\s+[-+\u2212]?\d+(?:[.,]\d+)?%?)$");

    static bool TryXuaExact(string text, out string tr)
    {
        tr = null;
        if (_xuaTranslator == null || _xuaTry == null) return false;
        object[] args = new object[] { text, null };
        if (!(bool)_xuaTry.Invoke(_xuaTranslator, args)) return false;
        tr = args[1] as string;
        return tr != null && tr != text && HasCyrillic(tr);
    }

    // Диагностика: файл BepInEx/config/Russificator.debug — первые 400 событий текста в журнал BepInEx.
    static int _debugLeft = -1;
    static readonly Dictionary<string, bool> _dbgFlags = new Dictionary<string, bool>();
    static bool DbgFlag(string name)
    {
        bool v;
        if (!_dbgFlags.TryGetValue(name, out v))
        {
            try { v = File.Exists(Path.Combine(Paths.ConfigPath, "Russificator." + name)); } catch { v = false; }
            _dbgFlags[name] = v;
        }
        return v;
    }

    static void Debug(string what, string text, string result)
    {
        if (_debugLeft < 0)
        {
            _debugLeft = 0;
            try { if (File.Exists(Path.Combine(Paths.ConfigPath, "Russificator.debug"))) _debugLeft = 400; } catch { }
        }
        if (_debugLeft <= 0) return;
        _debugLeft--;
        _log.LogInfo(what + ": " + (text ?? "<null>").Replace("\n", "\\n") + " => " + (result ?? "<null>").Replace("\n", "\\n"));
    }

    static void TextMeshPrefix(object __instance, ref string value)
    {
        if (_inside || _textMeshOff || value == null || value.Length < 2) return;
        if (DbgFlag("noprefix")) return;
        string before = value;
        try
        {
            FindXua();
            if (_xuaTranslator == null || !XuaTranslating())
            {
                Debug("TextMesh skip (xua " + (_xuaTranslator != null) + ")", value, null);
                return;
            }
            if (HasCyrillic(value))
            {
                // перевод (от XUnity или живой): строки — не длиннее строк английского оригинала
                State st0;
                if (_states.TryGetValue(KeyOf(__instance), out st0) && st0.Original != null)
                    value = FitLines(value, st0.Original);
                return;
            }
            if (!HasLatinWord(value)) return;
            StateOf(__instance).Original = value;
            string tr, flat;
            if (TryXua(value, out tr) || (Unwrap(value, out flat) && TryXua(flat, out tr)))
                value = FitLines(tr, value);
            Debug("TextMesh", before, value);
        }
        catch (Exception ex)
        {
            _textMeshOff = true;                 // сбой — эта часть плагина выключается, игра работает дальше
            _log.LogWarning("TextMesh translation helper disabled: " + ex.Message);
        }
    }
#endif

#if !IL2CPP
    // Текст сцены, заданный до установки перехватов (надпись на заставке), игра больше не меняет —
    // ни XUnity, ни перехват его не видят. Раз в секунду: включённые TextMesh с английским текстом,
    // у которого есть перевод в словаре XUnity, получают его. Только замена текста — без вызовов игры.
    static float _nextScan;
    static MethodInfo _findAll;
    static PropertyInfo _tmText, _tmActive;

    static void ScanTextMeshes()
    {
        if (_textMesh == null || _textMeshOff) return;
        if (DbgFlag("noscan")) return;
        float now = Time.realtimeSinceStartup;
        if (now < _nextScan) return;
        _nextScan = now + 1f;
        try
        {
            FindXua();
            if (_xuaTranslator == null || !XuaTranslating()) return;
            if (_findAll == null)
            {
                _findAll = FindObjectsMethod(_textMesh);
                _tmText = _textMesh.GetProperty("text");
                Type comp = FindType("UnityEngine.Component");
                PropertyInfo go = comp != null ? comp.GetProperty("gameObject") : null;
                _tmActive = go != null ? go.PropertyType.GetProperty("activeInHierarchy") : null;
                if (_findAll == null || _tmText == null) { _textMeshOff = true; return; }
            }
            Array all = _findAll.Invoke(null, new object[] { _textMesh }) as Array;
            if (all == null) return;
            int done = 0;
            foreach (object tm in all)
            {
                string text = _tmText.GetValue(tm, null) as string;
                if (text == null || text.Length < 2 || HasCyrillic(text) || !HasLatinWord(text)) continue;
                string tr, flat;
                if (!TryXua(text, out tr) && !(Unwrap(text, out flat) && TryXua(flat, out tr)))
                {
                    Debug("TextMesh scan miss", text, null);
                    continue;
                }
                Debug("TextMesh scan", text, tr);
                StateOf(tm).Original = text;
                _tmText.SetValue(tm, FitLines(tr, text), null);
                if (++done >= 50) break;
            }
        }
        catch (Exception ex)
        {
            _textMeshOff = true;
            _log.LogWarning("TextMesh scan disabled: " + ex.Message);
        }
    }
#endif

#if !IL2CPP
    // ---------- 6. Текстовые компоненты, которых XUnity не знает (ex2D exSpriteFont) ----------
    // XUnity их не видит: сеттер text перехватывается здесь, раз в секунду проверяются и тексты сцены.
    // Перевод — из словаря XUnity, а если его там нет — запросом к XUnity (живой перевод), ответ
    // ставится в тот же компонент, если игра за это время не сменила текст. Буквы для их растровых
    // шрифтов дорисовывает русификатор. tk2dTextMesh сюда не входит: его шрифты кириллицей пока не
    // дополняются — переведённый текст был бы пустым.
    static readonly string[] ForeignTextTypes = { "exSpriteFont" };
    static readonly List<Type> _foreign = new List<Type>();
    static readonly Dictionary<Type, PropertyInfo> _foreignText = new Dictionary<Type, PropertyInfo>();
    static readonly Dictionary<string, bool> _requested = new Dictionary<string, bool>();
    static MethodInfo _xuaAsync;
    static Type _xuaResult;
    static PropertyInfo _resOk, _resText;
    static bool _foreignOff;

    static void HookForeign(Harmony harmony)
    {
        foreach (string name in ForeignTextTypes)
        {
            Type t = null;
            foreach (System.Reflection.Assembly a in AppDomain.CurrentDomain.GetAssemblies())
            {
                try { t = a.GetType(name, false); } catch { }
                if (t != null) break;
            }
            if (t == null || _foreignText.ContainsKey(t)) continue;
            try
            {
                PropertyInfo text = t.GetProperty("text", BindingFlags.Instance | BindingFlags.Public);
                MethodInfo setter = text != null ? text.GetSetMethod() : null;
                if (setter == null || text.PropertyType != typeof(string)) continue;
                harmony.Patch(setter, new HarmonyMethod(Own("ForeignPrefix")));
                _foreign.Add(t);
                _foreignText[t] = text;
                _log.LogInfo(name + " hooks installed");
            }
            catch (Exception ex)
            {
                _log.LogWarning(name + " hooks failed: " + ex.Message);
            }
        }
    }

    static void ForeignPrefix(object __instance, ref string value)
    {
        if (_inside || _foreignOff || value == null || value.Length < 2) return;
        try
        {
            FindXua();
            if (_xuaTranslator == null || !XuaTranslating()) return;
            value = ForeignTranslate(__instance, value);
        }
        catch (Exception ex)
        {
            _foreignOff = true;
            _log.LogWarning("Text component helper disabled: " + ex.Message);
        }
    }

    // Перевод для компонента: готовый — сразу; нет в словаре — запрос, ответ придёт позже.
    static string ForeignTranslate(object inst, string value)
    {
        string r = ForeignTranslateCore(inst, value);
        Debug(inst.GetType().Name, value, r);
        return r;
    }

    static string ForeignTranslateCore(object inst, string value)
    {
        if (HasCyrillic(value))
        {
            State st0;
            if (_states.TryGetValue(KeyOf(inst), out st0) && st0.Original != null) return FitLines(value, st0.Original);
            return value;
        }
        if (!HasLatinWord(value)) return value;
        StateOf(inst).Original = value;
        string tr, flat;
        if (TryXua(value, out tr) || (Unwrap(value, out flat) && TryXua(flat, out tr))) return FitLines(tr, value);
        RequestAsync(inst, value);
        return value;
    }

    sealed class Pending
    {
        public object Target;
        public string Original;
        public PropertyInfo Prop;

        public void Done(object result)
        {
            try
            {
                if (result == null || _resOk == null || !(bool)_resOk.GetValue(result, null)) return;
                string tr = _resText.GetValue(result, null) as string;
                if (tr == null || !HasCyrillic(tr) || !Alive(Target) || !XuaTranslating()) return;
                if ((Prop.GetValue(Target, null) as string) != Original) return;   // игра уже сменила текст
                _inside = true;
                try { Prop.SetValue(Target, FitLines(tr, Original), null); }
                finally { _inside = false; }
            }
            catch { }
        }
    }

    static void RequestAsync(object inst, string text)
    {
        if (_requested.ContainsKey(text)) return;
        if (_xuaAsync == null)
        {
            Type itf = FindType("XUnity.AutoTranslator.Plugin.Core.ITranslator");
            _xuaResult = FindType("XUnity.AutoTranslator.Plugin.Core.TranslationResult");
            if (itf == null || _xuaResult == null) return;
            foreach (MethodInfo m in itf.GetMethods())
                if (m.Name == "TranslateAsync" && m.GetParameters().Length == 2 && m.GetParameters()[0].ParameterType == typeof(string))
                    _xuaAsync = m;
            _resOk = _xuaResult.GetProperty("Succeeded");
            _resText = _xuaResult.GetProperty("TranslatedText");
            if (_xuaAsync == null) return;
        }
        if (_requested.Count > 2000) _requested.Clear();
        _requested[text] = true;
        PropertyInfo prop;
        if (!_foreignText.TryGetValue(inst.GetType(), out prop))
        {
            foreach (KeyValuePair<Type, PropertyInfo> kv in _foreignText)
                if (kv.Key.IsInstanceOfType(inst)) { prop = kv.Value; break; }
        }
        if (prop == null) return;
        Pending p = new Pending { Target = inst, Original = text, Prop = prop };
        Type action = _xuaAsync.GetParameters()[1].ParameterType;
        Delegate cb = Delegate.CreateDelegate(action, p, typeof(Pending).GetMethod("Done"));
        _xuaAsync.Invoke(_xuaTranslator, new object[] { text, cb });
    }

    static float _nextForeignScan;

    // Тексты сцены, заданные до перехвата: раз в секунду — включённые компоненты с английским текстом.
    static void ScanForeign()
    {
        if (_foreign.Count == 0 || _foreignOff) return;
        float now = Time.realtimeSinceStartup;
        if (now < _nextForeignScan) return;
        _nextForeignScan = now + 1f;
        try
        {
            FindXua();
            if (_xuaTranslator == null || !XuaTranslating()) return;
            if (_findAll == null)
            {
                _findAll = FindObjectsMethod(_foreign[0]);
                if (_findAll == null) { _foreignOff = true; return; }
            }
            int done = 0;
            foreach (Type t in _foreign)
            {
                PropertyInfo prop = _foreignText[t];
                Array all = _findAll.Invoke(null, new object[] { t }) as Array;
                if (all == null) continue;
                foreach (object c in all)
                {
                    string text = prop.GetValue(c, null) as string;
                    if (text == null || text.Length < 2 || HasCyrillic(text) || !HasLatinWord(text)) continue;
                    string tr = ForeignTranslate(c, text);
                    if (tr == text) continue;
                    _inside = true;
                    try { prop.SetValue(c, tr, null); }
                    finally { _inside = false; }
                    if (++done >= 50) return;
                }
            }
        }
        catch (Exception ex)
        {
            _foreignOff = true;
            _log.LogWarning("Text component scan disabled: " + ex.Message);
        }
    }
#endif

    // UnityEngine.Object.FindObjectsOfType(Type) — из той же сборки, что и сам компонент (имя
    // UnityEngine.Object встречается и в других сборках, их метод вернёт пустой список).
    static MethodInfo FindObjectsMethod(Type component)
    {
        for (Type x = component; x != null; x = x.BaseType)
            if (x.FullName == "UnityEngine.Object")
                return x.GetMethod("FindObjectsOfType", BindingFlags.Static | BindingFlags.Public, null,
                                   new Type[] { typeof(Type) }, null);
        return null;
    }

#if !IL2CPP
    // ---------- 7. Перестройка текстуры динамического шрифта ----------
    // Русские буквы добавляются в текстуру шрифта по ходу игры; когда места нет, Unity перестраивает
    // её целиком. Надпись, неактивная в этот момент (у пунктов меню выбранный и обычный вид — разные
    // объекты), остаётся со старыми координатами букв и показывает мусор. После перестройки все
    // TextMesh с этим шрифтом (и неактивные) строятся заново.
    static readonly Dictionary<object, bool> _rebuiltFonts = new Dictionary<object, bool>();
    static bool _rebuildHooked;
    static MethodInfo _findAllIncl;
    static PropertyInfo _tmFont;

    static void HookFontRebuild()
    {
        if (_rebuildHooked) return;
        _rebuildHooked = true;
        try
        {
            Type font = FindType("UnityEngine.Font");
            EventInfo ev = font != null ? font.GetEvent("textureRebuilt", BindingFlags.Static | BindingFlags.Public) : null;
            if (ev == null) return;
            ev.AddEventHandler(null, Delegate.CreateDelegate(ev.EventHandlerType,
                typeof(RussificatorUnity).GetMethod("OnFontRebuilt", BindingFlags.Static | BindingFlags.NonPublic)));
        }
        catch (Exception ex)
        {
            _log.LogDebug("Font rebuild hook: " + ex.Message);
        }
    }

    static void OnFontRebuilt(object font)
    {
        if (font != null) _rebuiltFonts[font] = true;
        Debug("font texture rebuilt", font != null ? font.ToString() : null, null);
    }

    static void RefreshRebuilt()
    {
        if (_rebuiltFonts.Count == 0 || _textMesh == null || _textMeshOff) return;
        Dictionary<object, bool> fonts = new Dictionary<object, bool>(_rebuiltFonts);
        _rebuiltFonts.Clear();
        try
        {
            if (_findAllIncl == null)
            {
                Type res = FindType("UnityEngine.Resources");
                _findAllIncl = res != null ? res.GetMethod("FindObjectsOfTypeAll", BindingFlags.Static | BindingFlags.Public,
                                                           null, new Type[] { typeof(Type) }, null) : null;
                _tmFont = _textMesh.GetProperty("font");
                if (_tmText == null) _tmText = _textMesh.GetProperty("text");
                if (_findAllIncl == null || _tmFont == null || _tmText == null) return;
            }
            Array all = _findAllIncl.Invoke(null, new object[] { _textMesh }) as Array;
            if (all == null) return;
            int n = 0;
            _inside = true;
            try
            {
                foreach (object tm in all)
                {
                    object f = _tmFont.GetValue(tm, null);
                    if (f == null || !fonts.ContainsKey(f)) continue;
                    string text = _tmText.GetValue(tm, null) as string;
                    if (string.IsNullOrEmpty(text)) continue;
                    // тот же текст Unity не перестраивает — через другой и обратно
                    _tmText.SetValue(tm, text + " ", null);
                    _tmText.SetValue(tm, text, null);
                    n++;
                }
            }
            finally { _inside = false; }
            Debug("TextMesh refreshed after font rebuild", n.ToString(), null);
        }
        catch (Exception ex)
        {
            _log.LogDebug("Font rebuild refresh: " + ex.Message);
        }
    }
#endif

    static bool HasLatinWord(string s)
    {
        for (int i = 1; i < s.Length; i++)
            if (IsLatin(s[i]) && IsLatin(s[i - 1])) return true;
        return false;
    }

    static bool IsLatin(char c)
    {
        return (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z');
    }

    static string[] Lines(string s)
    {
        return s.Replace("\r\n", "\n").Split('\n');
    }

    // Текст строки без тегов разметки (<color=…>, <b>) — для длины и для проверки начала строки.
    static string Plain(string s)
    {
        StringBuilder sb = new StringBuilder(s.Length);
        bool tag = false;
        foreach (char c in s)
        {
            if (c == '<') { tag = true; continue; }
            if (c == '>' && tag) { tag = false; continue; }
            if (!tag) sb.Append(c);
        }
        return sb.ToString();
    }

    const string PhraseEnd = ".!?:;…»\"')]>}|*-–—";

    // Перенос посреди фразы: строка не кончается знаком конца фразы, следующая — со строчной буквы.
    static bool SoftBreak(string prev, string next)
    {
        string p = Plain(prev).TrimEnd();
        string n = Plain(next).TrimStart();
        if (p.Length == 0 || n.Length == 0) return false;
        if (PhraseEnd.IndexOf(p[p.Length - 1]) >= 0) return false;
        return char.IsLetter(n[0]) && char.IsLower(n[0]);
    }

    static bool Unwrap(string text, out string flat)
    {
        flat = text;
        string[] ls = Lines(text);
        if (ls.Length < 2) return false;
        string eol = text.Contains("\r\n") ? "\r\n" : "\n";
        StringBuilder sb = new StringBuilder(ls[0].TrimEnd());
        bool any = false;
        for (int i = 1; i < ls.Length; i++)
        {
            if (SoftBreak(ls[i - 1], ls[i])) { sb.Append(' ').Append(ls[i].Trim()); any = true; }
            else sb.Append(eol).Append(ls[i]);
        }
        flat = sb.ToString();
        return any;
    }

    // Строки перевода — не длиннее самой длинной строки оригинала, если оригинал игра переносила сама.
    static string FitLines(string tr, string orig)
    {
        string[] ol = Lines(orig);
        if (ol.Length < 2) return tr;
        bool soft = false;
        int max = 0;
        for (int i = 0; i < ol.Length; i++)
        {
            max = Math.Max(max, Plain(ol[i]).Trim().Length);
            if (i > 0 && SoftBreak(ol[i - 1], ol[i])) soft = true;
        }
        if (!soft || max < 8) return tr;
        string eol = orig.Contains("\r\n") ? "\r\n" : "\n";
        StringBuilder outp = new StringBuilder();
        string[] tl = Lines(tr);
        for (int i = 0; i < tl.Length; i++)
        {
            if (i > 0) outp.Append(eol);
            string line = tl[i];
            if (Plain(line).Length <= max || line.IndexOf(' ') < 0)
            {
                outp.Append(line);
                continue;
            }
            string[] words = line.Split(' ');
            int len = 0;
            for (int w = 0; w < words.Length; w++)
            {
                int wl = Plain(words[w]).Length;
                if (len > 0 && len + 1 + wl > max)
                {
                    outp.Append(eol);
                    len = 0;
                }
                else if (w > 0)
                {
                    outp.Append(' ');
                    len++;
                }
                outp.Append(words[w]);
                len += wl;
            }
        }
        return outp.ToString();
    }

    // ---------- 3. проверка, помещается ли перевод ----------

    static bool HasCyrillic(string s)
    {
        if (s == null) return false;
        for (int i = 0; i < s.Length; i++)
        {
            char c = s[i];
            if (c >= 'Ѐ' && c <= 'ӿ') return true;
        }
        return false;
    }

    static State StateOf(object inst)
    {
        State st;
        object key = KeyOf(inst);
        if (!_states.TryGetValue(key, out st))
        {
            if (_states.Count > 4000) _states.Clear();
            st = new State();
            _states[key] = st;
        }
        return st;
    }

    static void TextPrefix(object __instance, string value)
    {
        try
        {
            if (value == null || value.Length == 0) return;
            if (!HasCyrillic(value))
            {
                StateOf(__instance).Original = value;
                return;
            }
            // текст из сцены игра не присваивает — XUnity сразу пишет перевод поверх: оригинал ещё в компоненте
            State st = StateOf(__instance);
            if (st.Original == null)
            {
                string cur = ReadText(__instance, _tmp != null && _tmp.IsInstanceOfType(__instance));
                if (cur != null && cur.Length > 0 && !HasCyrillic(cur)) st.Original = cur;
            }
        }
        catch { }
    }

    static void TextPostfix(object __instance)
    {
        try
        {
            bool tmp = _tmp != null && _tmp.IsInstanceOfType(__instance);
            string s = ReadText(__instance, tmp);
            if (!HasCyrillic(s)) return;
            State st = StateOf(__instance);
            if (st.Fitted || s.Length <= st.CheckedLength) return;
            object key = KeyOf(__instance);
            if (!_queued.ContainsKey(key))
            {
                _queued[key] = true;
                _queue.Add(__instance);
            }
        }
        catch { }
    }

    // Раз в кадр: проверяем в следующем кадре — к этому времени Unity уже расставит размеры рамок.
    public static void Tick()
    {
#if !IL2CPP
        RefreshRebuilt();
        ScanTextMeshes();
        ScanForeign();
#endif
        if (_queue.Count == 0) return;
        int n = Math.Min(_queue.Count, 40);
        object[] batch = _queue.GetRange(0, n).ToArray();
        _queue.RemoveRange(0, n);
        foreach (object inst in batch)
        {
            try
            {
                _queued.Remove(KeyOf(inst));
                Check(inst);
            }
            catch (Exception ex) { _log.LogDebug("fit check failed: " + ex.Message); }
        }
    }

    static void Check(object inst)
    {
        if (!Alive(inst)) return;
        bool tmp = _tmp != null && _tmp.IsInstanceOfType(inst);
        string s = ReadText(inst, tmp);
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
            object key = KeyOf(inst);
            if (++st.Retries < 30 && !_queued.ContainsKey(key))
            {
                _queued[key] = true;
                _queue.Add(inst);      // рамка ещё не рассчитана — проверим позже
            }
            return;
        }
        st.CheckedLength = s.Length;
        if (tmp ? (bool)_tmpAuto.GetValue(inst, null) : (bool)_uguiBestFit.GetValue(inst, null)) return;
        if (KeepOneLine(inst, tmp, s, st.Original, w))
        {
            st.Fitted = true;
            if (_fitLogged++ < 100) _log.LogInfo("One line: " + NameOf(inst) + " — " + Shorten(s));
            return;
        }
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
        if (_fitLogged++ < 100) _log.LogInfo("Autosize: " + NameOf(inst) + " — " + Shorten(s));
    }

    static int _fitLogged;

    // Надпись, которая в оригинале помещалась в одну строку (кнопка, пункт меню, подпись над другой
    // надписью), а по-русски переносится: вторая строка наехала бы на соседние элементы, хотя в свою
    // рамку и влезает. Кегль уменьшается ровно настолько, чтобы перевод тоже встал в одну строку,
    // но не меньше MinScale (иначе пусть переносится).
    static bool KeepOneLine(object inst, bool tmp, string s, string original, float w)
    {
        if (original == null || s.IndexOf('\n') >= 0 || original.IndexOf('\n') >= 0) return false;
        float trans, orig;
        if (tmp)
        {
            if (!TmpWraps(inst)) return false;
            trans = (float)_tmpPrefW.GetValue(inst, null);       // текущий (русский) текст в одну строку
            int a = PlainLength(original), b = PlainLength(s);
            if (a <= 0 || b <= 0) return false;
            orig = trans * a / b;      // ширину оригинала мерить нельзя (портит буфер TMP) — по числу букв
        }
        else
        {
            if (_genPrefW == null || Convert.ToInt32(_uguiHOverflow.GetValue(inst, null)) != 0) return false;
            object gen = _uguiGen.GetValue(inst, null);
            float ppu = (float)_uguiPpu.GetValue(inst, null);
            if (gen == null || ppu <= 0f) return false;
            object extents = Activator.CreateInstance(_vector2, new object[] { w, 0f });
            object settings = _uguiSettings.Invoke(inst, new object[] { extents });
            trans = (float)_genPrefW.Invoke(gen, new object[] { s, settings }) / ppu;
            orig = (float)_genPrefW.Invoke(gen, new object[] { original, settings }) / ppu;
        }
        if (orig > w + 1.5f || trans <= w + 1.5f) return false;  // оригинал и так переносился / перевод влезает
        float k = (w - 1f) / trans;
        if (k < MinScale) return false;
        if (tmp)
        {
            float size = (float)_tmpSize.GetValue(inst, null);
            if (size <= 1f) return false;
            _tmpSize.SetValue(inst, (float)Math.Floor(size * k * 10.0) / 10f, null);
        }
        else
        {
            int size = (int)_uguiSize.GetValue(inst, null);
            int ns = (int)Math.Floor(size * k);
            if (ns < 1 || ns >= size) return false;
            _uguiSize.SetValue(inst, ns, null);
        }
        return true;
    }

    static int PlainLength(string s)
    {
        int n = 0;
        bool tag = false;
        foreach (char c in s)
        {
            if (c == '<') tag = true;
            else if (c == '>' && tag) tag = false;
            else if (!tag && !char.IsWhiteSpace(c)) n++;
        }
        return n;
    }

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
    // (В IL2CPP массивы textInfo — не System.Array: там эта проверка просто пропускается.)
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

#if IL2CPP
// Компонент в игре: у плагина BepInEx 6 нет своих Update/OnGUI — их даёт этот объект.
public class RussificatorBehaviour : MonoBehaviour
{
    public RussificatorBehaviour(IntPtr ptr) : base(ptr) { }

    public void Update()
    {
        RussificatorUnity.Tick();
    }

    public void OnGUI()
    {
        if (!RussificatorUnity.CreditActive) return;
        try { RussificatorUnity.DrawCredit(); }
        catch (Exception ex) { RussificatorUnity.StopCredit(ex); }
    }
}
#endif
