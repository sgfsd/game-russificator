// Заглушка UnityEngine.CoreModule (обёртка Il2CppInterop) — только для компиляции IL2CPP-плагина.
// Имена, наследование и сигнатуры — как в сборках, которые BepInEx 6 генерирует для игры.
namespace UnityEngine
{
    public class Object : Il2CppSystem.Object
    {
        public Object(System.IntPtr pointer) : base(pointer) { }
    }

    public class Component : Object
    {
        public Component(System.IntPtr pointer) : base(pointer) { }
    }

    public class Behaviour : Component
    {
        public Behaviour(System.IntPtr pointer) : base(pointer) { }
    }

    public class MonoBehaviour : Behaviour
    {
        public MonoBehaviour(System.IntPtr pointer) : base(pointer) { }
    }

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

    public sealed class Screen : Il2CppSystem.Object
    {
        public Screen(System.IntPtr pointer) : base(pointer) { }
        public static int width { get { return 0; } }
        public static int height { get { return 0; } }
    }

    public class Time : Il2CppSystem.Object
    {
        public Time(System.IntPtr pointer) : base(pointer) { }
        public static float realtimeSinceStartup { get { return 0f; } }
    }
}
