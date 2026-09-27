// Фикстура для tests/test_unity.py (il_scan.concat_templates): склейки строк в коде игры.
public class Hud {
    public string text;
    public int day, total, gold;
    public string player = "Ann";
    public void Show() {
        text = "Day " + day + " of " + total;                  // Concat(object, object, object, object)
        text = "Gold: " + gold.ToString();                     // Concat(string, string)
        text = "Welcome back, " + player + "! Ready?";         // Concat(string, string, string)
        text = "Level " + day + " — " + player + " has " + gold + " coins";   // Concat(string[])
        text = "a" + player;                                   // слишком мало букв — не шаблон
        text = player + total;                                 // без литералов — не шаблон
    }
    public string Branchy(bool b) {
        return "Mode: " + (b ? "on" : "off");                  // ветвление внутри — пропускаем
    }
}
