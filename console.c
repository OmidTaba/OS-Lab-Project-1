// Console input and output.
// Input is from the keyboard or serial port.
// Output is written to the screen and serial port.

#include "types.h"
#include "defs.h"
#include "param.h"
#include "traps.h"
#include "spinlock.h"
#include "sleeplock.h"
#include "fs.h"
#include "file.h"
#include "memlayout.h"
#include "mmu.h"
#include "proc.h"
#include "x86.h"

static void consputc(int);

static int panicked = 0;

static struct {
  struct spinlock lock;
  int locking;
} cons;

static void
printint(int xx, int base, int sign)
{
  static char digits[] = "0123456789abcdef";
  char buf[16];
  int i;
  uint x;

  if(sign && (sign = xx < 0))
    x = -xx;
  else
    x = xx;

  i = 0;
  do{
    buf[i++] = digits[x % base];
  }while((x /= base) != 0);

  if(sign)
    buf[i++] = '-';

  while(--i >= 0)
    consputc(buf[i]);
}
//PAGEBREAK: 50

// Print to the console. only understands %d, %x, %p, %s.
void
cprintf(char *fmt, ...)
{
  int i, c, locking;
  uint *argp;
  char *s;

  locking = cons.locking;
  if(locking)
    acquire(&cons.lock);

  if (fmt == 0)
    panic("null fmt");

  argp = (uint*)(void*)(&fmt + 1);
  for(i = 0; (c = fmt[i] & 0xff) != 0; i++){
    if(c != '%'){
      consputc(c);
      continue;
    }
    c = fmt[++i] & 0xff;
    if(c == 0)
      break;
    switch(c){
    case 'd':
      printint(*argp++, 10, 1);
      break;
    case 'x':
    case 'p':
      printint(*argp++, 16, 0);
      break;
    case 's':
      if((s = (char*)*argp++) == 0)
        s = "(null)";
      for(; *s; s++)
        consputc(*s);
      break;
    case '%':
      consputc('%');
      break;
    default:
      // Print unknown % sequence to draw attention.
      consputc('%');
      consputc(c);
      break;
    }
  }

  if(locking)
    release(&cons.lock);
}

void
panic(char *s)
{
  int i;
  uint pcs[10];

  cli();
  cons.locking = 0;
  // use lapiccpunum so that we can call panic from mycpu()
  cprintf("lapicid %d: panic: ", lapicid());
  cprintf(s);
  cprintf("\n");
  getcallerpcs(&s, pcs);
  for(i=0; i<10; i++)
    cprintf(" %p", pcs[i]);
  panicked = 1; // freeze other CPU
  for(;;)
    ;
}

//PAGEBREAK: 50
#define BACKSPACE 0x100
#define CRTPORT 0x3d4
static ushort *crt = (ushort*)P2V(0xb8000);  // CGA memory

static void
cgaputc_attr(int c, int attr)
{
  int pos;

  // Cursor position: col + 80*row.
  outb(CRTPORT, 14);
  pos = inb(CRTPORT+1) << 8;
  outb(CRTPORT, 15);
  pos |= inb(CRTPORT+1);

  if(c == '\n')
    pos += 80 - pos%80;
  else if(c == BACKSPACE){
    if(pos > 0) --pos;
  } else
    crt[pos++] = (c&0xff) | (attr & 0xff00);

  if(pos < 0 || pos > 25*80)
    panic("pos under/overflow");

  if((pos/80) >= 24){  // Scroll up.
    memmove(crt, crt+80, sizeof(crt[0])*23*80);
    pos -= 80;
    memset(crt+pos, 0, sizeof(crt[0])*(24*80 - pos));
  }

  outb(CRTPORT, 14);
  outb(CRTPORT+1, pos>>8);
  outb(CRTPORT, 15);
  outb(CRTPORT+1, pos);
  crt[pos] = ' ' | 0x0700;
}

static void
cgaputc(int c)
{
  cgaputc_attr(c, 0x0700);
}

void
consputc(int c)
{
  if(panicked){
    cli();
    for(;;)
      ;
  }

  if(c == BACKSPACE){
    uartputc('\b'); uartputc(' '); uartputc('\b');
  } else
    uartputc(c);
  cgaputc(c);
}

#define KEY_LF 0xE4
#define KEY_RT 0xE5

static void
cgacursormove(int delta)
{
  int pos;
  outb(CRTPORT, 14);
  pos = inb(CRTPORT+1) << 8;
  outb(CRTPORT, 15);
  pos |= inb(CRTPORT+1);

  pos += delta;
  if(pos < 0)
    pos = 0;
  if(pos >= 25*80)
    pos = 25*80 - 1;

  outb(CRTPORT, 14);
  outb(CRTPORT+1, pos >> 8);
  outb(CRTPORT, 15);
  outb(CRTPORT+1, pos);
}

static void
conscursorleft(void)
{
  cgacursormove(-1);
  uartputc('\b');
}

static void
conscursorright(char c)
{
  cgacursormove(1);
  uartputc('\033');
  uartputc('[');
  uartputc('C');
}

#define INPUT_BUF 128
struct {
  char buf[INPUT_BUF];
  uint r;  // Read index
  uint w;  // Write index
  uint e;  // Edit index
  uint cpos; // Cursor position index
} input;

// Selection & Clipboard state
static char clipboard[INPUT_BUF];
static int clip_len = 0;
static int sel_active = 0;     // 1 while picking range between 1st & 2nd Ctrl+I
static int has_selection = 0;  // 1 when selection is active & highlighted
static uint sel_anchor = 0;
static uint sel_start = 0;
static uint sel_end = 0;

static void
redraw_line(int screen_cpos, int old_len)
{
  int back_steps = screen_cpos - input.w;
  for(int i = 0; i < back_steps; i++){
    conscursorleft();
  }

  uint sel_l = 0, sel_h = 0;
  if(has_selection){
    sel_l = sel_start;
    sel_h = sel_end;
  }

  int in_highlight = 0;
  for(uint i = input.w; i < input.e; i++){
    int is_sel = (has_selection && i >= sel_l && i < sel_h);
    if(is_sel && !in_highlight){
      uartputc('\033'); uartputc('['); uartputc('7'); uartputc('m');
      in_highlight = 1;
    } else if(!is_sel && in_highlight){
      uartputc('\033'); uartputc('['); uartputc('0'); uartputc('m');
      in_highlight = 0;
    }

    char ch = input.buf[i % INPUT_BUF];
    uartputc(ch);
    cgaputc_attr(ch, is_sel ? 0x7000 : 0x0700);
  }

  if(in_highlight){
    uartputc('\033'); uartputc('['); uartputc('0'); uartputc('m');
    in_highlight = 0;
  }

  int current_len = input.e - input.w;
  int clear_count = old_len - current_len;
  if(clear_count > 0){
    for(int i = 0; i < clear_count; i++){
      uartputc(' ');
      cgaputc_attr(' ', 0x0700);
    }
    for(int i = 0; i < clear_count; i++){
      conscursorleft();
    }
  }

  int forward_steps = input.e - input.cpos;
  for(int i = 0; i < forward_steps; i++){
    conscursorleft();
  }
}

#define C(x)  ((x)-'@')  // Control-x

void
consoleintr(int (*getc)(void))
{
  int c, doprocdump = 0;
  static int esc_state = 0;

  acquire(&cons.lock);
  while((c = getc()) >= 0){
    // Handle ANSI escape sequences for arrow keys from terminal/serial
    if(esc_state == 1){
      if(c == '['){
        esc_state = 2;
        continue;
      }
      esc_state = 0;
    } else if(esc_state == 2){
      esc_state = 0;
      if(c == 'D'){
        c = KEY_LF;
      } else if(c == 'C'){
        c = KEY_RT;
      } else {
        continue;
      }
    } else if(c == 0x1B){ // ESC
      esc_state = 1;
      continue;
    }

    switch(c){
    case C('P'):  // Process listing.
      doprocdump = 1;
      break;

    case C('U'):  // Kill line.
      if(sel_active)
        break;
      if(has_selection){
        has_selection = 0;
        redraw_line(input.cpos, input.e - input.w);
      }
      while(input.cpos < input.e){
        conscursorright(input.buf[input.cpos % INPUT_BUF]);
        input.cpos++;
      }
      while(input.e != input.w &&
            input.buf[(input.e-1) % INPUT_BUF] != '\n'){
        input.e--;
        input.cpos--;
        consputc(BACKSPACE);
      }
      break;

    case KEY_LF:  // Move cursor left
      if(has_selection){
        has_selection = 0;
        redraw_line(input.cpos, input.e - input.w);
      }
      if(input.cpos > input.w){
        input.cpos--;
        conscursorleft();
      }
      break;

    case KEY_RT:  // Move cursor right
      if(has_selection){
        has_selection = 0;
        redraw_line(input.cpos, input.e - input.w);
      }
      if(input.cpos < input.e){
        char ch = input.buf[input.cpos % INPUT_BUF];
        input.cpos++;
        conscursorright(ch);
      }
      break;

    case C('Q'):  // Move cursor to beginning of line
      if(has_selection){
        has_selection = 0;
        redraw_line(input.cpos, input.e - input.w);
      }
      while(input.cpos > input.w){
        input.cpos--;
        conscursorleft();
      }
      break;

    case C('E'):  // Move cursor to end of line
      if(has_selection){
        has_selection = 0;
        redraw_line(input.cpos, input.e - input.w);
      }
      while(input.cpos < input.e){
        char ch = input.buf[input.cpos % INPUT_BUF];
        input.cpos++;
        conscursorright(ch);
      }
      break;

    case C('I'):  // Select start / end toggle
      if(!sel_active){
        sel_active = 1;
        sel_anchor = input.cpos;
        if(has_selection){
          has_selection = 0;
          redraw_line(input.cpos, input.e - input.w);
        }
      } else {
        sel_active = 0;
        if(input.cpos != sel_anchor){
          has_selection = 1;
          sel_start = (sel_anchor < input.cpos) ? sel_anchor : input.cpos;
          sel_end = (sel_anchor < input.cpos) ? input.cpos : sel_anchor;
          redraw_line(input.cpos, input.e - input.w);
        } else {
          has_selection = 0;
          redraw_line(input.cpos, input.e - input.w);
        }
      }
      break;

    case C('C'):  // Copy selection to clipboard
      if(sel_active)
        break;
      if(has_selection && sel_start != sel_end){
        clip_len = 0;
        for(uint i = sel_start; i < sel_end; i++){
          clipboard[clip_len++] = input.buf[i % INPUT_BUF];
        }
        clipboard[clip_len] = 0;
        // Selection remains active and highlighted
      }
      break;

    case C('V'):  // Paste clipboard
      if(sel_active)
        break;
      if(clip_len > 0){
        int old_len = input.e - input.w;
        int screen_cpos = input.cpos;
        if(has_selection && sel_start != sel_end){
          int k = sel_end - sel_start;
          for(uint i = sel_end; i < input.e; i++){
            input.buf[(i - k) % INPUT_BUF] = input.buf[i % INPUT_BUF];
          }
          input.e -= k;
          input.cpos = sel_start;
          screen_cpos = sel_start;
          has_selection = 0;
        }
        if(input.e - input.r + clip_len < INPUT_BUF){
          for(uint i = input.e + clip_len - 1; i >= input.cpos + clip_len; i--){
            input.buf[i % INPUT_BUF] = input.buf[(i - clip_len) % INPUT_BUF];
          }
          for(int i = 0; i < clip_len; i++){
            input.buf[(input.cpos + i) % INPUT_BUF] = clipboard[i];
          }
          input.e += clip_len;
          input.cpos += clip_len;
        }
        has_selection = 0;
        sel_active = 0;
        redraw_line(screen_cpos, old_len);
      }
      break;

    case C('H'): case '\x7f':  // Backspace
      if(sel_active)
        break;
      if(has_selection && sel_start != sel_end){
        int old_len = input.e - input.w;
        int screen_cpos = input.cpos;
        int k = sel_end - sel_start;
        for(uint i = sel_end; i < input.e; i++){
          input.buf[(i - k) % INPUT_BUF] = input.buf[i % INPUT_BUF];
        }
        input.e -= k;
        input.cpos = sel_start;
        has_selection = 0;
        sel_active = 0;
        redraw_line(screen_cpos, old_len);
      } else if(input.cpos > input.w){
        int old_len = input.e - input.w;
        int screen_cpos = input.cpos;
        for(uint i = input.cpos; i < input.e; i++){
          input.buf[(i - 1) % INPUT_BUF] = input.buf[i % INPUT_BUF];
        }
        input.e--;
        input.cpos--;
        redraw_line(screen_cpos, old_len);
      }
      break;

    default:
      if(sel_active)
        break;
      if(c != 0 && input.e-input.r < INPUT_BUF){
        c = (c == '\r') ? '\n' : c;
        if(c == '\n' || c == C('D')){
          if(has_selection){
            has_selection = 0;
            redraw_line(input.cpos, input.e - input.w);
          }
          while(input.cpos < input.e){
            char ch = input.buf[input.cpos % INPUT_BUF];
            input.cpos++;
            conscursorright(ch);
          }
          input.buf[input.e++ % INPUT_BUF] = c;
          input.cpos = input.e;
          consputc(c);
          input.w = input.e;
          wakeup(&input.r);
        } else {
          int old_len = input.e - input.w;
          int screen_cpos = input.cpos;
          if(has_selection && sel_start != sel_end){
            int k = sel_end - sel_start;
            for(uint i = sel_end; i < input.e; i++){
              input.buf[(i - k) % INPUT_BUF] = input.buf[i % INPUT_BUF];
            }
            input.e -= k;
            input.cpos = sel_start;
            screen_cpos = sel_start;
            has_selection = 0;
          }
          for(uint i = input.e; i > input.cpos; i--){
            input.buf[i % INPUT_BUF] = input.buf[(i - 1) % INPUT_BUF];
          }
          input.buf[input.cpos % INPUT_BUF] = c;
          input.e++;
          input.cpos++;
          redraw_line(screen_cpos, old_len);
        }
      }
      break;
    }
  }
  release(&cons.lock);
  if(doprocdump) {
    procdump();  // now call procdump() wo. cons.lock held
  }
}

int
consoleread(struct inode *ip, char *dst, int n)
{
  uint target;
  int c;

  iunlock(ip);
  target = n;
  acquire(&cons.lock);
  while(n > 0){
    while(input.r == input.w){
      if(myproc()->killed){
        release(&cons.lock);
        ilock(ip);
        return -1;
      }
      sleep(&input.r, &cons.lock);
    }
    c = input.buf[input.r++ % INPUT_BUF];
    if(c == C('D')){  // EOF
      if(n < target){
        // Save ^D for next time, to make sure
        // caller gets a 0-byte result.
        input.r--;
      }
      break;
    }
    *dst++ = c;
    --n;
    if(c == '\n')
      break;
  }
  release(&cons.lock);
  ilock(ip);

  return target - n;
}

int
consolewrite(struct inode *ip, char *buf, int n)
{
  int i;

  iunlock(ip);
  acquire(&cons.lock);
  for(i = 0; i < n; i++)
    consputc(buf[i] & 0xff);
  release(&cons.lock);
  ilock(ip);

  return n;
}

void
consoleinit(void)
{
  initlock(&cons.lock, "console");

  devsw[CONSOLE].write = consolewrite;
  devsw[CONSOLE].read = consoleread;
  cons.locking = 1;

  ioapicenable(IRQ_KBD, 0);
}

