//=============================================================================
// Russificator.js — добавлен русификатором игр (Game Russificator)
//=============================================================================
/*:
 * @plugindesc Русификатор: перенос слов и подгонка шрифта под русский текст.
 * @author Game Russificator
 * @help
 * Добавлен автоматически при русификации игры. Настраивать не нужно.
 *
 * Русский текст длиннее английского. Плагин:
 *  - переносит слово на новую строку, если оно не помещается в окно сообщения,
 *    прокручиваемого текста или справки (движок сам откроет следующую страницу);
 *  - уменьшает шрифт описания в окне справки (до 60%), если оно не влезает;
 *  - уменьшает шрифт строки журнала боя, если она шире окна.
 * Пока текст помещается, ничего не меняется. Любая ошибка внутри плагина
 * возвращает стандартное поведение движка.
 */

(function () {
    'use strict';

    var MZ = typeof Utils !== 'undefined' && Utils.RPGMAKER_NAME === 'MZ';
    var MIN_SCALE = 0.6;
    var ESCAPE_TAIL = /(?:\x1b(?:[A-Z]+(?:\[[^\]]*\]|<[^>]*>)?|[\$\.\|\^!><\{\}\\]))+$/i;

    function contentsWidth(win) {
        if (win.contentsWidth) return win.contentsWidth();
        return win.innerWidth || (win.contents ? win.contents.width : 0);
    }

    // символ перед позицией i, не считая управляющих кодов (\C[2] и т.п.)
    function previousChar(text, i) {
        var before = text.slice(Math.max(0, i - 48), i);
        var m = ESCAPE_TAIL.exec(before);
        var j = m ? i - m[0].length : i;
        return j > 0 ? text.charAt(j - 1) : '\n';
    }

    function isWordStart(text, i) {
        var c = text.charAt(i);
        if (!c || c === ' ' || c === '\n' || c === '\x1b' || c.charCodeAt(0) < 0x20) return false;
        var p = previousChar(text, i);
        return p === ' ' || p === '\n';
    }

    function wordAt(text, i) {
        var m = /^[^\s\x1b]+/.exec(text.slice(i));
        return m ? m[0] : '';
    }

    // ---------- перенос слов ----------

    if (!MZ) {
        var _processNormalCharacter = Window_Base.prototype.processNormalCharacter;
        Window_Base.prototype.processNormalCharacter = function (textState) {
            try {
                if (this._rusWrap && textState && typeof textState.text === 'string') {
                    var i = textState.index, t = textState.text;
                    if (isWordStart(t, i) && textState.x > textState.left) {
                        var w = this.textWidth(wordAt(t, i));
                        if (textState.x + w > contentsWidth(this)) {
                            // вставляем обычный перевод строки: движок обработает его сам
                            // (в окне сообщения — с новой страницей, если строки кончились)
                            textState.text = t.slice(0, i) + '\n' + t.slice(i);
                            return;
                        }
                    }
                }
            } catch (e) {
                // стандартное поведение
            }
            _processNormalCharacter.call(this, textState);
        };
    } else {
        var _processCharacter = Window_Base.prototype.processCharacter;
        Window_Base.prototype.processCharacter = function (textState) {
            try {
                if (this._rusWrap && textState && !textState.rtl && typeof textState.text === 'string') {
                    var i = textState.index, t = textState.text;
                    if (isWordStart(t, i)) {
                        this.flushTextState(textState);
                        if (textState.x > textState.startX) {
                            var w = this.textWidth(wordAt(t, i));
                            if (textState.x + w > contentsWidth(this)) {
                                textState.text = t.slice(0, i) + '\n' + t.slice(i);
                            }
                        }
                    }
                }
            } catch (e) {
                // стандартное поведение
            }
            _processCharacter.call(this, textState);
        };
    }

    [typeof Window_Message !== 'undefined' ? Window_Message : null,
     typeof Window_ScrollText !== 'undefined' ? Window_ScrollText : null,
     typeof Window_Help !== 'undefined' ? Window_Help : null].forEach(function (cls) {
        if (cls) cls.prototype._rusWrap = true;
    });

    // ---------- подгонка размера шрифта ----------

    var _resetFontSettings = Window_Base.prototype.resetFontSettings;
    Window_Base.prototype.resetFontSettings = function () {
        _resetFontSettings.call(this);
        if (this._rusFontSize && this.contents) this.contents.fontSize = this._rusFontSize;
    };

    function baseFontSize(win) {
        if (MZ) return $gameSystem.mainFontSize();
        return win.standardFontSize();
    }

    function lineHeightFor(win, size) {
        if (MZ) return size + Math.max(0, win.lineHeight() - $gameSystem.mainFontSize());
        return size + 8;
    }

    function plainText(win, text) {
        var conv = win.convertEscapeCharacters(String(text || ''));
        conv = conv.replace(/\x1bI\[\d+\]/gi, '　　');
        return conv.replace(/\x1b[A-Z]+(?:\[[^\]]*\]|<[^>]*>)?/gi, '').replace(/\x1b./g, '');
    }

    function countLines(win, text, size, limit) {
        var saved = win.contents.fontSize;
        win.contents.fontSize = size;
        var lines = 0;
        plainText(win, text).split('\n').forEach(function (para) {
            var x = 0;
            lines++;
            para.split(' ').forEach(function (word, k) {
                var w = win.textWidth((k > 0 ? ' ' : '') + word);
                if (x > 0 && x + w > limit) {
                    lines++;
                    x = win.textWidth(word);
                } else {
                    x += w;
                }
            });
        });
        win.contents.fontSize = saved;
        return lines;
    }

    if (typeof Window_Help !== 'undefined') {
        var _helpRefresh = Window_Help.prototype.refresh;
        Window_Help.prototype.refresh = function () {
            this._rusFontSize = 0;
            try {
                if (this.contents && this._text) {
                    var base = baseFontSize(this);
                    var height = MZ ? this.innerHeight : this.contentsHeight();
                    var limit = contentsWidth(this) - 16;
                    var size = base, min = Math.floor(base * MIN_SCALE);
                    while (size > min && countLines(this, this._text, size, limit) * lineHeightFor(this, size) > height) {
                        size--;
                    }
                    if (size < base) this._rusFontSize = size;
                }
            } catch (e) {
                this._rusFontSize = 0;
            }
            _helpRefresh.call(this);
        };
    }

    // ---------- надпись о программе (только в русификаторах «для друзей») ----------

    var params = {};
    try {
        if (typeof PluginManager !== 'undefined' && PluginManager.parameters) {
            params = PluginManager.parameters('Russificator') || {};
        }
    } catch (e) {
        params = {};
    }
    var CREDIT = String(params.credit || '');

    if (CREDIT && typeof Scene_Title !== 'undefined') {
        var _titleCreate = Scene_Title.prototype.create;
        Scene_Title.prototype.create = function () {
            _titleCreate.apply(this, arguments);
            try {
                var w = Graphics.width, size = Math.max(12, Math.round(Graphics.height / 42)), h = size + 14;
                var bmp = new Bitmap(w, h);
                bmp.fontSize = size;
                if (MZ && $gameSystem && $gameSystem.mainFontFace) bmp.fontFace = $gameSystem.mainFontFace();
                bmp.textColor = 'rgba(255,255,255,0.85)';
                bmp.outlineColor = 'rgba(0,0,0,0.65)';
                bmp.outlineWidth = 3;
                bmp.drawText(CREDIT, 0, 0, w - 14, h, 'right');
                var sprite = new Sprite(bmp);
                sprite.y = Graphics.height - h - 2;
                this.addChild(sprite);
            } catch (e) {
                // надпись необязательна
            }
        };
    }

    if (typeof Window_BattleLog !== 'undefined') {
        var _drawLineText = Window_BattleLog.prototype.drawLineText;
        Window_BattleLog.prototype.drawLineText = function (index) {
            this._rusFontSize = 0;
            try {
                var rect = this.lineRect ? this.lineRect(index) : this.itemRectForText(index);
                var text = plainText(this, this._lines[index]);
                var base = baseFontSize(this);
                var saved = this.contents.fontSize, size = base, min = Math.floor(base * MIN_SCALE);
                this.contents.fontSize = size;
                while (size > min && this.textWidth(text) > rect.width) {
                    size--;
                    this.contents.fontSize = size;
                }
                this.contents.fontSize = saved;
                if (size < base) this._rusFontSize = size;
            } catch (e) {
                this._rusFontSize = 0;
            }
            _drawLineText.call(this, index);
            this._rusFontSize = 0;
        };
    }
})();
