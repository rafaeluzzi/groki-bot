# Panels

One panel on the Portal screen, next to the orb. A new `show_panel` replaces whatever is open. The same `id` updates it in place, with no exit animation. `dismiss_panel` closes it.

`ttl_s` is optional seconds. Omit it for the default. `0` stays until dismissed. Defaults: list 45, choice 60, webview 30. `now_playing` does not time out. A touch restarts the timer. The timer starts when speech stops, so the card is not closed mid-sentence.

A tap that is not `dismiss` or `local:` is a user turn. The panel closes when that tap is sent. Answer it by voice. Do not ask the person to repeat the tap.

A Spotify card can sit under another panel and comes back when that panel closes.

## Which kind

- Places, shows, episodes, or other rows: `list`
- Yes/no, or 2 to 4 options: `choice`. Call `show_panel` in that same turn and speak one short line. Example: `{"version":1,"kind":"choice","prompt":"Podcasts or Spotify?","options":["Podcasts","Spotify"]}`
- A fact, table, forecast, score, steps, or bullets: `webview` with a template
- Music that is playing: `now_playing`

## Fields

- list: `title`, `items` (a string or `{title}` / `{label}`, optional `subtitle`, optional `image` or `art_url` as an `https` picture). At most 12. A picture that cannot be fetched is left off the row.
- choice: `prompt`, `options` (1 to 4 labels).
- now_playing: `title` and `show` (or artist). Pause and play stay on the Portal.
- webview: `template` and `data`, or an `html` fragment. Prefer a template. No scripts and no page URLs. An `https` image on a fact is fetched for you and inlined. Koji does not load a remote image itself.

## Templates

Pass `kind`, `template`, and `data`. Optional `actions` is 1 or 2 `{id, label}` buttons. The id comes back with the tap.

weather: `{"kind":"webview","template":"weather","data":{"place":"Durham","temp":"72°","condition":"Mild","high":"75","low":"61"}}`

compare: `{"kind":"webview","template":"compare","data":{"title":"Shoes","columns":["A","B"],"rows":[{"label":"Weight","values":["10 oz","9 oz"]}]}}`

agenda: `{"kind":"webview","template":"agenda","data":{"title":"Today","items":[{"when":"9:00","title":"Standup"}]}}`

steps: `{"kind":"webview","template":"steps","data":{"title":"Tea","steps":["Boil water","Steep four minutes"]}}`

fact: `{"kind":"webview","template":"fact","data":{"title":"Durham","text":"A city in North Carolina."}}`

score: `{"kind":"webview","template":"score","data":{"title":"Final","left":{"name":"Home","score":"2"},"right":{"name":"Away","score":"1"}}}`

key_points: `{"kind":"webview","template":"key_points","data":{"title":"Summary","points":["Bring a coat","Leave by 4"]}}`

chart: `{"kind":"webview","template":"chart","data":{"title":"Week","type":"bar","labels":["Mon","Tue","Wed"],"values":[1,3,2]}}`

`type` is `bar` or `line`. One series, 1 to 8 points. The gateway draws a small PNG. Do not send SVG or HTML for the chart.
