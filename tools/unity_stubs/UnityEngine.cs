// Заглушка UnityEngine.dll — ТОЛЬКО для компиляции плагина Russificator.Unity (BepInEx 5, Mono).
//
// Во время игры ссылки уходят в настоящий UnityEngine.dll игры (в Unity 2017.2+ это фасад,
// перенаправляющий типы в модули), поэтому здесь объявлены только используемые члены, с теми же
// именами и сигнатурами (структуры — структурами, перечисления — перечислениями), что в Unity 5–6.
// Тела пустые: заглушка в игру не попадает.

namespace UnityEngine
{
    public class Object { }
    public class Component : Object { }
    public class Behaviour : Component { }
    public class MonoBehaviour : Behaviour { }

    public struct Rect
    {
        public Rect(float x, float y, float width, float height) { }
    }

    public struct Color
    {
        public float r;
        public float g;
        public float b;
        public float a;
    }

    public enum TextAnchor
    {
        UpperLeft = 0, UpperCenter = 1, UpperRight = 2, MiddleLeft = 3, MiddleCenter = 4, MiddleRight = 5,
        LowerLeft = 6, LowerCenter = 7, LowerRight = 8
    }

    public enum FontStyle { Normal = 0, Bold = 1, Italic = 2, BoldAndItalic = 3 }

    public sealed class GUIStyleState
    {
        public Color textColor { get { return default(Color); } set { } }
    }

    public sealed class GUIStyle
    {
        public GUIStyle() { }
        public int fontSize { get { return 0; } set { } }
        public TextAnchor alignment { get { return TextAnchor.UpperLeft; } set { } }
        public FontStyle fontStyle { get { return FontStyle.Normal; } set { } }
        public bool wordWrap { get { return false; } set { } }
        public GUIStyleState normal { get { return null; } set { } }
    }

    public class GUI
    {
        public static void Label(Rect position, string text, GUIStyle style) { }
    }

    public sealed class Screen
    {
        public static int width { get { return 0; } }
        public static int height { get { return 0; } }
    }

    public sealed class Time
    {
        public static float realtimeSinceStartup { get { return 0f; } }
    }
}
