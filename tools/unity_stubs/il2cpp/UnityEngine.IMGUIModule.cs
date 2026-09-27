// Заглушка UnityEngine.IMGUIModule (обёртка Il2CppInterop) — только для компиляции.
namespace UnityEngine
{
    public sealed class GUIStyleState : Il2CppSystem.Object
    {
        public GUIStyleState(System.IntPtr pointer) : base(pointer) { }
        public Color textColor { get { return default(Color); } set { } }
    }

    public sealed class GUIStyle : Il2CppSystem.Object
    {
        public GUIStyle(System.IntPtr pointer) : base(pointer) { }
        public GUIStyle() : base(System.IntPtr.Zero) { }
        public int fontSize { get { return 0; } set { } }
        public TextAnchor alignment { get { return TextAnchor.UpperLeft; } set { } }
        public bool wordWrap { get { return false; } set { } }
        public GUIStyleState normal { get { return null; } set { } }
    }

    public class GUI : Il2CppSystem.Object
    {
        public GUI(System.IntPtr pointer) : base(pointer) { }
        public static void Label(Rect position, string text, GUIStyle style) { }
    }
}
