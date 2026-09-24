define e = Character("Eileen", color="#c8ffc8")
define m = Character(_("Mary"))
define gui.text_font = "fonts/LatinOnly.ttf"

screen hello_screen():
    vbox:
        text "Welcome to the test screen"
        textbutton _("Start the adventure") action Return()
        add "gui/bg.png"

label start:
    "It was a dark and stormy night."
    e "Hello, [player]! How are you today?"
    e "I'm {b}really{/b} happy to see you.{w} Let's go!"
    m "Wait for me!\nI'm coming."
    menu:
        "Where should we go?"
        "Go to the forest":
            e "The forest it is."
        "Stay at home":
            m "Good choice."
    $ renpy.notify(_("Game saved"))
    return
