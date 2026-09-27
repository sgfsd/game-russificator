// Проверка resources/rpgmaker/Russificator.js на моделях окон RPG Maker MV и MZ.
// Модели повторяют логику вывода текста стандартных скриптов движка (rpg_windows.js /
// rmmz_windows.js): посимвольный вывод, перенос строки, сброс шрифта, окна справки и боя.
// Запуск: node tests/js/rpgmaker_plugin_test.js  (вызывается из tests/test_engines.py)
'use strict';
var fs = require('fs');
var path = require('path');
var assert = require('assert');
var vm = require('vm');

var PLUGIN = fs.readFileSync(path.join(__dirname, '..', '..', 'russificator', 'resources', 'rpgmaker',
    'Russificator.js'), 'utf8');
var CHAR = 0.5;           // ширина символа — полкегля (как моноширинный шрифт)

function Bitmap(width, height) { this.width = width; this.height = height; this.fontSize = 28; }
Bitmap.prototype.measureTextWidth = function (t) { return t.length * this.fontSize * CHAR; };

function makeMV() {
    var g = { Utils: { RPGMAKER_NAME: 'MV' } };
    function Window_Base() { this.contents = new Bitmap(400, 144); this.drawn = []; }
    Window_Base.prototype.standardFontSize = function () { return 28; };
    Window_Base.prototype.contentsWidth = function () { return this.contents.width; };
    Window_Base.prototype.contentsHeight = function () { return this.contents.height; };
    Window_Base.prototype.textWidth = function (t) { return this.contents.measureTextWidth(t); };
    Window_Base.prototype.resetFontSettings = function () { this.contents.fontSize = this.standardFontSize(); };
    Window_Base.prototype.convertEscapeCharacters = function (t) { return t.replace(/\\/g, '\x1b'); };
    Window_Base.prototype.processCharacter = function (ts) {
        switch (ts.text[ts.index]) {
            case '\n': this.processNewLine(ts); break;
            case '\x1b':
                var m = /^\x1b([A-Z]+)(\[\d+\])?/i.exec(ts.text.slice(ts.index));
                ts.index += m[0].length;
                break;
            default: this.processNormalCharacter(ts);
        }
    };
    Window_Base.prototype.processNormalCharacter = function (ts) {
        var c = ts.text[ts.index++];
        var w = this.textWidth(c);
        this.drawn.push({ c: c, x: ts.x, y: ts.y, w: w });
        ts.x += w;
    };
    Window_Base.prototype.processNewLine = function (ts) {
        ts.x = ts.left;
        ts.y += this.contents.fontSize + 8;
        ts.index++;
    };
    Window_Base.prototype.drawTextEx = function (text, x, y) {
        this.resetFontSettings();
        var ts = { index: 0, x: x, y: y, left: x, text: this.convertEscapeCharacters(text) };
        while (ts.index < ts.text.length) this.processCharacter(ts);
        return ts;
    };
    function sub(parent) {
        function W() { parent.call(this); }
        W.prototype = Object.create(parent.prototype);
        W.prototype.constructor = W;
        return W;
    }
    var Window_Message = sub(Window_Base);
    var Window_ScrollText = sub(Window_Base);
    var Window_Help = sub(Window_Base);
    Window_Help.prototype.setText = function (t) { this._text = t; this.refresh(); };
    Window_Help.prototype.refresh = function () { this.drawn = []; this.drawTextEx(this._text, 4, 0); };
    var Window_BattleLog = sub(Window_Base);
    Window_BattleLog.prototype.itemRectForText = function () { return { x: 0, y: 0, width: 300, height: 36 }; };
    Window_BattleLog.prototype.drawLineText = function (index) {
        var rect = this.itemRectForText(index);
        this.drawn = [];
        this.drawTextEx(this._lines[index], rect.x, rect.y, rect.width);
    };
    g.Window_Base = Window_Base; g.Window_Message = Window_Message; g.Window_ScrollText = Window_ScrollText;
    g.Window_Help = Window_Help; g.Window_BattleLog = Window_BattleLog;
    return g;
}

function makeMZ() {
    var g = { Utils: { RPGMAKER_NAME: 'MZ' }, $gameSystem: { mainFontSize: function () { return 26; } } };
    function Window_Base() { this.contents = new Bitmap(400, 144); this.contents.fontSize = 26; this.drawn = []; }
    Object.defineProperty(Window_Base.prototype, 'innerWidth', { get: function () { return this.contents.width; } });
    Object.defineProperty(Window_Base.prototype, 'innerHeight', { get: function () { return this.contents.height; } });
    Window_Base.prototype.lineHeight = function () { return 36; };
    Window_Base.prototype.textWidth = function (t) { return this.contents.measureTextWidth(t); };
    Window_Base.prototype.resetFontSettings = function () { this.contents.fontSize = g.$gameSystem.mainFontSize(); };
    Window_Base.prototype.convertEscapeCharacters = function (t) { return t.replace(/\\/g, '\x1b'); };
    Window_Base.prototype.createTextState = function (text, x, y) {
        return { text: this.convertEscapeCharacters(text), index: 0, x: x, y: y, startX: x, startY: y,
                 buffer: '', drawing: true, rtl: false, height: 36 };
    };
    Window_Base.prototype.processCharacter = function (ts) {
        var c = ts.text[ts.index++];
        if (c.charCodeAt(0) < 0x20) {
            this.flushTextState(ts);
            if (c === '\n') this.processNewLine(ts);
            if (c === '\x1b') {
                var m = /^([A-Z]+)(\[\d+\])?/i.exec(ts.text.slice(ts.index));
                ts.index += m[0].length;
            }
        } else {
            ts.buffer += c;
        }
    };
    Window_Base.prototype.flushTextState = function (ts) {
        var w = this.textWidth(ts.buffer);
        if (ts.buffer) this.drawn.push({ c: ts.buffer, x: ts.x, y: ts.y, w: w });
        ts.x += w;
        ts.buffer = '';
    };
    Window_Base.prototype.processNewLine = function (ts) {
        ts.x = ts.startX;
        ts.y += this.contents.fontSize + 10;
    };
    Window_Base.prototype.drawTextEx = function (text, x, y) {
        this.resetFontSettings();
        var ts = this.createTextState(text, x, y);
        while (ts.index < ts.text.length) this.processCharacter(ts);
        this.flushTextState(ts);
        return ts;
    };
    function sub(parent) {
        function W() { parent.call(this); }
        W.prototype = Object.create(parent.prototype);
        return W;
    }
    var Window_Message = sub(Window_Base);
    var Window_Help = sub(Window_Base);
    Window_Help.prototype.refresh = function () { this.drawn = []; this.drawTextEx(this._text, 0, 0); };
    var Window_BattleLog = sub(Window_Base);
    Window_BattleLog.prototype.lineRect = function () { return { x: 0, y: 0, width: 300, height: 36 }; };
    Window_BattleLog.prototype.drawLineText = function (index) {
        var rect = this.lineRect(index);
        this.drawn = [];
        this.drawTextEx(this._lines[index], rect.x, rect.y, rect.width);
    };
    g.Window_Base = Window_Base; g.Window_Message = Window_Message; g.Window_Help = Window_Help;
    g.Window_BattleLog = Window_BattleLog;
    return g;
}

function load(g) {
    vm.createContext(g);
    vm.runInContext(PLUGIN, g);
    return g;
}

// слова не рвутся, ничего не выходит за правый край
function checkLayout(drawn, width, text) {
    drawn.forEach(function (d) {    // пробел в конце строки невидим — его край не считаем
        var ink = d.c.replace(/\s+$/, '');
        assert.ok(!ink || d.x + d.w * ink.length / d.c.length <= width + 0.01, 'за край: ' + JSON.stringify(d));
    });
    var rows = {};
    drawn.forEach(function (d) { rows[d.y] = (rows[d.y] || '') + d.c; });
    var lines = Object.keys(rows).map(Number).sort(function (a, b) { return a - b; }).map(function (y) {
        return rows[y].trim();
    });
    assert.strictEqual(lines.join(' ').replace(/\s+/g, ' '), text.replace(/\\C\[\d+\]/g, ''));
    return lines;
}

var TEXT = 'Прохладный ветер донёс запах дождя, и \\C[2]путники\\C[0] поспешили укрыться под старым мостом.';

// --- MV ---
var mv = load(makeMV());
var win = new mv.Window_Message();
win.drawTextEx(TEXT, 0, 0);
var lines = checkLayout(win.drawn, 400, TEXT);
assert.ok(lines.length >= 3, 'MV: перенос по словам');

var plain = new mv.Window_Base();          // обычные окна плагин не трогает
plain.drawTextEx('short text here', 0, 0);
assert.strictEqual(plain.drawn.length, 'short text here'.length);

var help = new mv.Window_Help();
help.setText('Очень длинное описание предмета, которое никак не помещается в две строки окна справки и требует меньшего шрифта.');
assert.ok(help._rusFontSize > 0 && help._rusFontSize < 28 && help._rusFontSize >= 16, 'MV: шрифт справки ' + help._rusFontSize);
help.setText('Лечит 50 HP.');
assert.strictEqual(help._rusFontSize, 0, 'MV: короткое описание — без изменений');

var log = new mv.Window_BattleLog();
log._lines = ['Гарольд атакует противника!'];
log.drawLineText(0);
var right = Math.max.apply(null, log.drawn.map(function (d) { return d.x + d.w; }));
assert.ok(right <= 300 + 0.01, 'MV: строка боя в окне, ' + right);

// --- MZ ---
var mz = load(makeMZ());
var wz = new mz.Window_Message();
wz.drawTextEx(TEXT, 0, 0);
var lz = checkLayout(wz.drawn, 400, TEXT);
assert.ok(lz.length >= 3, 'MZ: перенос по словам');

var hz = new mz.Window_Help();
hz._text = 'Очень длинное описание предмета, которое никак не помещается в две строки окна справки и требует меньшего шрифта.';
hz.refresh();
assert.ok(hz._rusFontSize > 0 && hz._rusFontSize < 26, 'MZ: шрифт справки ' + hz._rusFontSize);

var lzb = new mz.Window_BattleLog();
lzb._lines = ['Гарольд атакует противника!'];
lzb.drawLineText(0);
var rz = Math.max.apply(null, lzb.drawn.map(function (d) { return d.x + d.w; }));
assert.ok(rz <= 300 + 0.01, 'MZ: строка боя в окне, ' + rz);

// --- Alt+T: оригинал реплик и выборов; надпись о программе на титульном экране ---
(function () {
    var g = makeMV();
    var handlers = [];
    g.document = { addEventListener: function (t, fn) { if (t === 'keydown') handlers.push(fn); },
                   getElementById: function () { return null; },
                   createElement: function () { return { style: {} }; }, body: { appendChild: function () {} } };
    g.setTimeout = function () { return 0; };
    g.clearTimeout = function () {};
    g.XMLHttpRequest = function () {};
    g.XMLHttpRequest.prototype.open = function () {};
    g.XMLHttpRequest.prototype.overrideMimeType = function () {};
    g.XMLHttpRequest.prototype.send = function () {
        this.status = 200;
        this.responseText = JSON.stringify({ m: { 'Привет,\nпутник!': 'Hello,\ntraveler!' }, c: { 'Да': 'Yes' } });
        this.onload();
    };
    function Game_Message() { this._texts = ['Привет,', 'путник!']; this._choices = ['Да', 'Нет']; }
    Game_Message.prototype.allText = function () { return this._texts.join('\n'); };
    Game_Message.prototype.choices = function () { return this._choices; };
    g.Game_Message = Game_Message;
    var drawn = [];
    function Bitmap2(w, h) { this.width = w; this.height = h; }
    Bitmap2.prototype.drawText = function (t) { drawn.push(t); };
    g.Bitmap = Bitmap2;
    g.Sprite = function (b) { this.bitmap = b; };
    g.Graphics = { width: 816, height: 624 };
    function Scene_Title() { this.children = []; }
    Scene_Title.prototype.create = function () {};
    Scene_Title.prototype.addChild = function (c) { this.children.push(c); };
    g.Scene_Title = Scene_Title;
    g.PluginManager = { parameters: function () { return { credit: 'Автоперевод: Русификатор игр' }; } };
    load(g);
    var msg = new g.Game_Message();
    assert.strictEqual(msg.allText(), 'Привет,\nпутник!', 'по умолчанию — перевод');
    handlers[0]({ altKey: true, ctrlKey: false, keyCode: 84, preventDefault: function () {} });
    assert.strictEqual(msg.allText(), 'Hello,\ntraveler!', 'Alt+T — оригинал реплики');
    assert.deepStrictEqual(msg.choices(), ['Yes', 'Нет'], 'Alt+T — оригинал выборов');
    handlers[0]({ altKey: true, ctrlKey: false, keyCode: 84, preventDefault: function () {} });
    assert.strictEqual(msg.allText(), 'Привет,\nпутник!', 'снова перевод');
    var title = new g.Scene_Title();
    title.create();
    assert.strictEqual(title.children.length, 1, 'надпись добавлена на титульный экран');
    assert.deepStrictEqual(drawn, ['Автоперевод: Русификатор игр']);
})();

console.log('OK');
