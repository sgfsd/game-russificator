// Фикстура для tests/test_unity.py (il_scan): два вида «печатной машинки».
public class Label {
    public string text { get; set; }
    public int maxVisibleCharacters { get; set; }
}
public class AppendTyper {          // text += буква (читает текст обратно)
    public Label label = new Label();
    public void Type(string line) {
        foreach (char c in line.ToCharArray()) label.text = label.text + c.ToString();
    }
}
public class RevealTyper {          // maxVisibleCharacters до text.Length
    public Label label = new Label();
    public void Step(int i) {
        if (i <= label.text.Length) label.maxVisibleCharacters = i;
    }
}
