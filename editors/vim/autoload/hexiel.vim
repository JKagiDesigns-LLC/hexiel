vim9script
# hexiel for Vim 9: a chat split on top of `hexiel --acp` (Agent Client Protocol, JSON lines over a job's stdio).

var job: job
var session = ''
var next_id = 1
var pending: dict<func> = {}
var chat_bufnr = -1
var busy = false
var open_line = false
var diffs: list<dict<any>> = []

def Command(): list<string>
  var c = get(g:, 'hexiel_command', ['hexiel', '--acp'])
  return type(c) == v:t_string ? split(c) : c
enddef

def ChatBuf(): number
  if chat_bufnr > 0 && bufexists(chat_bufnr)
    return chat_bufnr
  endif
  chat_bufnr = bufadd('hexiel://chat')
  bufload(chat_bufnr)
  setbufvar(chat_bufnr, '&buftype', 'nofile')
  setbufvar(chat_bufnr, '&swapfile', 0)
  setbufvar(chat_bufnr, '&filetype', 'markdown')
  setbufvar(chat_bufnr, '&bufhidden', 'hide')
  return chat_bufnr
enddef

# add text to the chat; `open_line` keeps streamed chunks on one line
def Append(text: string)
  var buf = ChatBuf()
  var pieces = split(text, "\n", true)
  var last = getbufinfo(buf)[0].linecount
  if open_line
    pieces[0] = getbufline(buf, last)[0] .. pieces[0]
    setbufline(buf, last, pieces[0])
    if len(pieces) > 1
      appendbufline(buf, last, pieces[1 :])
    endif
  elseif last == 1 && getbufline(buf, 1)[0] == ''
    setbufline(buf, 1, pieces[0])
    if len(pieces) > 1
      appendbufline(buf, 1, pieces[1 :])
    endif
  else
    appendbufline(buf, last, pieces)
  endif
  open_line = true
  var win = bufwinid(buf)
  if win > 0
    win_execute(win, 'normal! G')
  endif
enddef

def Line(text: string)
  if open_line
    Append("\n")
  endif
  Append(text)
  open_line = false
enddef

def Send(obj: dict<any>)
  if job_status(job) ==# 'run'
    ch_sendraw(job, json_encode(obj) .. "\n")
  endif
enddef

def Request(method: string, params: dict<any>, Cb: func)
  var id = next_id
  next_id += 1
  pending[string(id)] = Cb
  Send({jsonrpc: '2.0', id: id, method: method, params: params})
enddef

def OnPermission(msg: dict<any>)
  var title = get(get(msg.params, 'toolCall', {}), 'title', 'a tool')
  var options: list<dict<any>> = get(msg.params, 'options', [])
  var menu = ['hexiel wants to: ' .. title]
  var i = 1
  for o in options
    add(menu, i .. '. ' .. o.name)
    i += 1
  endfor
  var pick = inputlist(menu)
  if pick >= 1 && pick <= len(options)
    Send({jsonrpc: '2.0', id: msg.id, result: {outcome: {outcome: 'selected', optionId: options[pick - 1].optionId}}})
  else
    Send({jsonrpc: '2.0', id: msg.id, result: {outcome: {outcome: 'cancelled'}}})
  endif
enddef

def OnUpdate(u: dict<any>)
  var kind = get(u, 'sessionUpdate', '')
  if kind == 'agent_message_chunk'
    Append(get(u.content, 'text', ''))
  elseif kind == 'tool_call'
    Line('⏺ ' .. get(u, 'title', 'tool'))
  elseif kind == 'tool_call_update'
    for c in get(u, 'content', [])
      if get(c, 'type', '') == 'diff'
        add(diffs, c)
        Line('  ± ' .. get(c, 'path', '?') .. '   (:HexielDiff to view)')
      endif
    endfor
    if get(u, 'status', '') == 'failed'
      Line('  ✗ failed')
    endif
  endif
enddef

def Handle(msg: dict<any>)
  if !has_key(msg, 'method')
    var key = string(get(msg, 'id', ''))
    if has_key(pending, key)
      var Cb = pending[key]
      remove(pending, key)
      Cb(get(msg, 'result', {}), get(msg, 'error', {}))
    endif
  elseif msg.method == 'session/update'
    OnUpdate(msg.params.update)
  elseif msg.method == 'session/request_permission'
    OnPermission(msg)
  elseif has_key(msg, 'id')
    Send({jsonrpc: '2.0', id: msg.id, error: {code: -32601, message: 'not supported: ' .. msg.method}})
  endif
enddef

def OnOut(ch: channel, line: string)
  if line =~ '^\s*$'
    return
  endif
  var msg: any
  try
    msg = json_decode(line)
  catch
    return
  endtry
  if type(msg) == v:t_dict
    Handle(msg)
  endif
enddef

def OnExit(j: job, status: number)
  session = ''
  busy = false
  Line('— hexiel stopped (exit ' .. status .. ')')
enddef

def Start(Done: func)
  if session != '' && job_status(job) ==# 'run'
    Done()
    return
  endif
  pending = {}
  session = ''
  job = job_start(Command(), {mode: 'nl', out_cb: OnOut, exit_cb: OnExit, err_io: 'null', cwd: getcwd()})
  if job_status(job) !=# 'run'
    busy = false
    echohl ErrorMsg | echom 'hexiel: could not start `' .. join(Command(), ' ') .. '` - is it installed? (pipx install hexiel)' | echohl None
    return
  endif
  Request('initialize', {protocolVersion: 1, clientCapabilities: {}, clientInfo: {name: 'hexiel.vim', version: '0.1.0'}},
    (res, err) => {
      if !empty(err)
        echom 'hexiel: ' .. get(err, 'message', 'initialize failed')
        return
      endif
      Request('session/new', {cwd: getcwd(), mcpServers: []}, (r2, e2) => {
        if !empty(e2) || !has_key(r2, 'sessionId')
          echom 'hexiel: ' .. get(e2, 'message', 'no session')
          return
        endif
        session = r2.sessionId
        Done()
      })
    })
enddef

export def Open()
  var buf = ChatBuf()
  if bufwinid(buf) < 0
    execute 'botright vertical sbuffer ' .. buf
    execute 'vertical resize ' .. get(g:, 'hexiel_width', 70)
    setlocal wrap linebreak nonumber
    nnoremap <buffer> q <Cmd>close<CR>
    nnoremap <buffer> i <Cmd>HexielAsk<CR>
  endif
enddef

export def Prompt(text: string)
  if busy
    echo 'hexiel is still working (:HexielStop to interrupt)'
    return
  endif
  var cur = win_getid()
  Open()
  Line('')
  Line('❯ ' .. substitute(text, "\n", "\n  ", 'g'))
  Line('')
  win_gotoid(cur)
  busy = true
  Start(() => {
    Request('session/prompt', {sessionId: session, prompt: [{type: 'text', text: text}]}, (res, err) => {
      busy = false
      if !empty(err)
        Line('⚠ ' .. get(err, 'message', 'error'))
      elseif get(res, 'stopReason', '') == 'cancelled'
        Line('(stopped)')
      endif
    })
  })
enddef

export def Ask(question: string)
  var q = question
  if q == ''
    q = input('hexiel> ')
  endif
  if q != ''
    Prompt(q)
  endif
enddef

# the lines of a :range as a fenced block with the file name and line numbers, plus the question
export def SendRange(line1: number, line2: number, question: string)
  var q = question
  if q == ''
    q = input('hexiel (about the selection)> ', 'Explain this.')
  endif
  if q == ''
    return
  endif
  var body = join(getline(line1, line2), "\n")
  Prompt(printf("%s\n\n%s (lines %d-%d):\n```%s\n%s\n```", q, fnamemodify(expand('%'), ':.'), line1, line2, &filetype, body))
enddef

export def Stop()
  if session != ''
    Send({jsonrpc: '2.0', method: 'session/cancel', params: {sessionId: session}})
  endif
enddef

export def NewChat()
  if job_status(job) ==# 'run'
    job_stop(job)
  endif
  session = ''
  busy = false
  diffs = []
  open_line = false
  if chat_bufnr > 0 && bufexists(chat_bufnr)
    deletebufline(chat_bufnr, 1, '$')
  endif
enddef

export def Diff()
  if empty(diffs)
    echo 'hexiel: no edit to show yet'
    return
  endif
  var d = diffs[-1]
  tabnew
  setlocal buftype=nofile bufhidden=wipe
  setline(1, split(get(d, 'oldText', ''), "\n", true))
  diffthis
  vertical new
  setlocal buftype=nofile bufhidden=wipe
  setline(1, split(get(d, 'newText', ''), "\n", true))
  diffthis
  echo 'hexiel: ' .. get(d, 'path', '') .. ' - left: before, right: after'
enddef

# for tests: the chat's text
export def ChatText(): list<string>
  return chat_bufnr > 0 ? getbufline(chat_bufnr, 1, '$') : []
enddef

export def Busy(): bool
  return busy
enddef
