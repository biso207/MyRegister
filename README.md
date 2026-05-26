# MyRegister

MyRegister is a local grade manager for school and university students. it uses a plain html/css/js frontend and a small python server with a local sqlite database.

## start

```powershell
python app.py
```

then open:

```text
http://127.0.0.1:8000
```

the sqlite database is created automatically at:

```text
data/myregister.sqlite3
```

## features

- create subjects with a school scale (`/10`), university scale (`/30`), or percentage scale (`/100`)
- add grades with description, optional percentage weight, and optional reference date
- keep the selected subject while adding multiple grades in the same session
- view the overall weighted average as a plain normalized value in the home page
- view all subjects in a clean vertical list
- compare subjects with a radar chart
- open a subject popup with all grades, descriptions, dates, last modification date, and single-subject average
- switch the interface language between english, italian, spanish, french, and german
- edit or delete subjects and grades

## structure

```text
app.py                 python server, api, and sqlite logic
public/index.html      app layout
public/css/styles.css  visual design and responsive layout
public/js/app.js       frontend state, api calls, rendering, and chart
data/                  local sqlite database folder
```
