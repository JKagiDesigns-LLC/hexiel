" vim -u NONE -N -es -S editors/test/vim_test.vim   (run from the repo root; needs python3)
set nocompatible
set runtimepath^=editors/vim
runtime plugin/hexiel.vim
let g:hexiel_command = ['python3', 'editors/test/fake_agent.py']
let s:errors = []
function! s:Check(cond, msg)
  if !a:cond | call add(s:errors, a:msg) | endif
endfunction
" a permission prompt is answered by inputlist(): feed "1<CR>" (Allow)
call feedkeys("1\<CR>", 't')
call setline(1, ['alpha', 'beta', 'gamma'])
2,3HexielSend what is this?
for i in range(60)
  sleep 50m
  if !hexiel#Busy() && !empty(hexiel#ChatText()) && join(hexiel#ChatText(), "\n") =~ 'choice=' | break | endif
endfor
let s:text = join(hexiel#ChatText(), "\n")
call s:Check(s:text =~ 'You said: what is this?', 'reply quotes the question: ' . s:text)
call s:Check(s:text =~ 'beta', 'selection was sent: ' . s:text)
call s:Check(s:text =~ '⏺ edit a.txt', 'tool card shown')
call s:Check(s:text =~ 'choice=allow', 'permission answered Allow: ' . s:text)
call s:Check(s:text =~ '± /tmp/a.txt', 'diff noted')
call s:Check(!hexiel#Busy(), 'not busy at the end')
HexielDiff
call s:Check(&diff, 'diff view opens')
call writefile(empty(s:errors) ? ['ok: vim plugin'] : s:errors, '/dev/stdout')
qa!
