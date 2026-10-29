// Shell.

#include "types.h"
#include "user.h"
#include "fcntl.h"

// Parsed command representation
#define EXEC  1
#define REDIR 2
#define PIPE  3
#define LIST  4
#define BACK  5

#define MAXARGS 10

#define MAX_ALIAS 20
#define MAX_LENGTH 100

#define MAX_HISTORY 20

struct cmd {
  int type;
};

struct execcmd {
  int type;
  char *argv[MAXARGS];
  char *eargv[MAXARGS];
};

struct redircmd {
  int type;
  struct cmd *cmd;
  char *file;
  char *efile;
  int mode;
  int fd;
};

struct pipecmd {
  int type;
  struct cmd *left;
  struct cmd *right;
};

struct listcmd {
  int type;
  struct cmd *left;
  struct cmd *right;
};

struct backcmd {
  int type;
  struct cmd *cmd;
};

int fork1(void);  // Fork but panics on failure.
void panic(char*);
struct cmd *parsecmd(char*);

// Execute cmd.  Never returns.
void
runcmd(struct cmd *cmd)
{
  int p[2];
  struct backcmd *bcmd;
  struct execcmd *ecmd;
  struct listcmd *lcmd;
  struct pipecmd *pcmd;
  struct redircmd *rcmd;

  if(cmd == 0)
    exit();

  switch(cmd->type){
  default:
    panic("runcmd");

  case EXEC:
    ecmd = (struct execcmd*)cmd;
    if(ecmd->argv[0] == 0)
      exit();
    exec(ecmd->argv[0], ecmd->argv);
    printf(2, "exec %s failed\n", ecmd->argv[0]);
    break;

  case REDIR:
    rcmd = (struct redircmd*)cmd;
    close(rcmd->fd);
    if(open(rcmd->file, rcmd->mode) < 0){
      printf(2, "open %s failed\n", rcmd->file);
      exit();
    }
    runcmd(rcmd->cmd);
    break;

  case LIST:
    lcmd = (struct listcmd*)cmd;
    if(fork1() == 0)
      runcmd(lcmd->left);
    wait();
    runcmd(lcmd->right);
    break;

  case PIPE:
    pcmd = (struct pipecmd*)cmd;
    if(pipe(p) < 0)
      panic("pipe");
    if(fork1() == 0){
      close(1);
      dup(p[1]);
      close(p[0]);
      close(p[1]);
      runcmd(pcmd->left);
    }
    if(fork1() == 0){
      close(0);
      dup(p[0]);
      close(p[0]);
      close(p[1]);
      runcmd(pcmd->right);
    }
    close(p[0]);
    close(p[1]);
    wait();
    wait();
    break;

  case BACK:
    bcmd = (struct backcmd*)cmd;
    if(fork1() == 0)
      runcmd(bcmd->cmd);
    break;
  }
  exit();
}

int
getcmd(char *buf, int nbuf)
{
  printf(2, "$ ");
  memset(buf, 0, nbuf);
  gets(buf, nbuf);
  if(buf[0] == 0) // EOF
    return -1;
  return 0;
}

struct alias_entry {
    char name[32];
    char cmd[128];
};

struct alias_entry aliases[MAX_ALIAS];
int alias_count = 0;

struct history_entry {
  int id;
  char cmd[128];
};

struct history_entry history[MAX_HISTORY];
int hist_count = 0;
int next_hist_id = 1;

void addToHistory(char *cmd) {
  if (cmd[0] == 0 || cmd[0] == '\n')
    return;

  if (hist_count == MAX_HISTORY)
  {
    // Shifting all the history entries to the left
    for (int i = 0; i < MAX_HISTORY - 1; i++)
      history[i] = history[i + 1];

    hist_count--;
  }

  history[hist_count].id = next_hist_id++;

  int len = strlen(cmd);
  if (len >= sizeof(history[hist_count].cmd))
    len = sizeof(history[hist_count].cmd) - 1;

  memmove(history[hist_count].cmd, cmd, len);
  history[hist_count].cmd[len] = 0; // Null terminating

  if (len > 0 && history[hist_count].cmd[len - 1] == '\n')
    history[hist_count].cmd[len - 1] = 0;

  hist_count++;
}

int
main(void)
{
  static char buf[100];
  int fd;

  // Ensure that three file descriptors are open.
  while((fd = open("console", O_RDWR)) >= 0){
    if(fd >= 3){
      close(fd);
      break;
    }
  }

  // Read and run input commands.
  while(getcmd(buf, sizeof(buf)) >= 0){
    if(buf[0] == 'c' && buf[1] == 'd' && buf[2] == ' '){
      // Chdir must be called by the parent, not the child.
      buf[strlen(buf)-1] = 0;  // chop \n
      if(chdir(buf+3) < 0)
        printf(2, "cannot cd %s\n", buf+3);
      continue;
    }

    // -------------------- History Expansion Command --------------------

    // Step 1: Checking for the history command
    if (buf[0] == 'h' && buf[1] == 'i' && buf[2] == 's' && buf[3] == 't' &&
        buf[4] == 'o' && buf[5] == 'r' && buf[6] == 'y' &&
       (buf[7] == '\n' || buf[7] == 0 || buf[7] == ' ')) {

      if (hist_count == 0)
        printf(1, "No command history.\n");
      else
        for (int i = 0; i < hist_count; i++)
          printf(1, "%d. %s\n", history[i].id, history[i].cmd);

      continue;
    }

    // Step 2: Doing history expansion
    if (buf[0] == '!') {
      char *target_cmd = 0;

      int is_num = 1, curr = 1;
      while (buf[curr] != ' ' && buf[curr] != '\n' && buf[curr] != 0) {
        if (buf[curr] < '0' || buf[curr] > '9') {
          is_num = 0; // Found non-digit character
          break;
        }
        curr++;
      }

      // Condition 1: !!
      if (buf[1] == '!') {
        if(hist_count > 0)
          target_cmd = history[hist_count - 1].cmd;
      }

      // Condition 2: !n
      else if (is_num && curr > 1) {
        int target_id = atoi(buf + 1);

        for (int i = 0; i < hist_count; i++) {
          if (target_id == history[i].id) {
            target_cmd = history[i].cmd;
            break;
          }
        }
      }

      // Condition 3: !prefix
      else {
        char prefix[64];
        int j = 0;

        // Extracting the prefix
        while (buf[1 + j] != 0 && buf[1 + j] != '\n' && buf[1 + j] != ' ' && j < 63) {
          prefix[j] = buf[1 + j];
          j++;
        }
        prefix[j] = 0;

        for (int i = hist_count - 1; i >= 0; i--) {
          int match = 1, curr = 0;

          while (prefix[curr] != 0) {
            if (history[i].cmd[curr] != prefix[curr]) {
              match = 0;
              break;
            }
            curr++;
          }

          if (match) {
            target_cmd = history[i].cmd;
            break;
          }
        }
      }

      // Step 3: Checking for possibly entered arguments and adding them to buffer
      if (target_cmd != 0) {
        int skip = 1;
        while (buf[skip] != ' ' && buf[skip] != '\n' && buf[skip] != 0)
          skip++;

        char expanded_buf[MAX_LENGTH];
        memmove(expanded_buf, target_cmd, sizeof(expanded_buf));

        if (buf[skip] != 0 && buf[skip] != '\n') {
          int curr_len = strlen(expanded_buf);
          memmove(expanded_buf + curr_len, buf + skip, sizeof(expanded_buf) - curr_len);
        }
        else {
          int curr_len = strlen(expanded_buf);
          expanded_buf[curr_len] = '\n';
          expanded_buf[curr_len + 1] = 0;
        }

        printf(1, "%s", expanded_buf);

        memmove(buf, expanded_buf, sizeof(buf));
      }
      else {
        printf(2, "history: command not found\n");
        continue;
      }
    }

    // Step 4: Adding to history
    addToHistory(buf);

    // -------------------- Alias Command --------------------

    // ----- Step 1: Checking and storing aliases -----
    if (buf[0] == 'a' && buf[1] == 'l' && buf[2] == 'i' && buf[3] == 'a' && buf[4] == 's' && buf[5] == ' ') {

      char *eq = strchr(buf, '=');
      if (eq) {
        *eq = 0; // Dividing the buffer into two sections before and after the '='
        char *name = buf + 6; // Reading after the 'alias ' (6 chars) until the '='
        char *cmd = eq + 1;

        // Removing the newline character
        int len = strlen(cmd);
        if (len > 0 && cmd[len - 1] == '\n')
          cmd[len - 1] = 0;

        // Checking if the alias name already exists
        int found = 0;
        for (int i = 0; i < alias_count; i++) {
          if (!strcmp(aliases[i].name, name)) {
            memmove(aliases[i].cmd, cmd, strlen(cmd) + 1);
            found = 1;
            break;
          }
        }

        // Adding the new alias to the list of aliases
        if (!found && alias_count < MAX_ALIAS) {
          memmove(aliases[alias_count].name, name, strlen(name) + 1);
          memmove(aliases[alias_count].cmd, cmd, strlen(cmd) + 1);
          alias_count++;
        }
      }
      continue;
    }

    // ----- Step 2: Replacing user's entered command with aliases if possible -----
    char first_word[MAX_LENGTH];
    int i = 0;

    while (buf[i] != ' ' && buf[i] != '\n' && buf[i] != 0 && i < MAX_LENGTH - 1) {
      first_word[i] = buf[i];
      i++;
    }
    first_word[i] = 0; // null-terminating the first_word

    for (int j = 0; j < alias_count; j++) {

      if (!strcmp(aliases[j].name, first_word)) {
        char new_buf[MAX_LENGTH];
        memmove(new_buf, aliases[j].cmd, strlen(aliases[j].cmd) + 1);

        // Adding possibly entered arguments to the end of buffer
        if (buf[i] != 0) {
          memmove(new_buf + strlen(new_buf), buf + i, strlen(buf + i) + 1);
        }
        else {
          memmove(new_buf + strlen(new_buf), "\n", 2);
        }

        memmove(buf, new_buf, strlen(new_buf) + 1);
        break;
      }
    }

    if(fork1() == 0)
      runcmd(parsecmd(buf));
    wait();
  }
  exit();
}

void
panic(char *s)
{
  printf(2, "%s\n", s);
  exit();
}

int
fork1(void)
{
  int pid;

  pid = fork();
  if(pid == -1)
    panic("fork");
  return pid;
}

//PAGEBREAK!
// Constructors

struct cmd*
execcmd(void)
{
  struct execcmd *cmd;

  cmd = malloc(sizeof(*cmd));
  memset(cmd, 0, sizeof(*cmd));
  cmd->type = EXEC;
  return (struct cmd*)cmd;
}

struct cmd*
redircmd(struct cmd *subcmd, char *file, char *efile, int mode, int fd)
{
  struct redircmd *cmd;

  cmd = malloc(sizeof(*cmd));
  memset(cmd, 0, sizeof(*cmd));
  cmd->type = REDIR;
  cmd->cmd = subcmd;
  cmd->file = file;
  cmd->efile = efile;
  cmd->mode = mode;
  cmd->fd = fd;
  return (struct cmd*)cmd;
}

struct cmd*
pipecmd(struct cmd *left, struct cmd *right)
{
  struct pipecmd *cmd;

  cmd = malloc(sizeof(*cmd));
  memset(cmd, 0, sizeof(*cmd));
  cmd->type = PIPE;
  cmd->left = left;
  cmd->right = right;
  return (struct cmd*)cmd;
}

struct cmd*
listcmd(struct cmd *left, struct cmd *right)
{
  struct listcmd *cmd;

  cmd = malloc(sizeof(*cmd));
  memset(cmd, 0, sizeof(*cmd));
  cmd->type = LIST;
  cmd->left = left;
  cmd->right = right;
  return (struct cmd*)cmd;
}

struct cmd*
backcmd(struct cmd *subcmd)
{
  struct backcmd *cmd;

  cmd = malloc(sizeof(*cmd));
  memset(cmd, 0, sizeof(*cmd));
  cmd->type = BACK;
  cmd->cmd = subcmd;
  return (struct cmd*)cmd;
}
//PAGEBREAK!
// Parsing

char whitespace[] = " \t\r\n\v";
char symbols[] = "<|>&;()";

int
gettoken(char **ps, char *es, char **q, char **eq)
{
  char *s;
  int ret;

  s = *ps;
  while(s < es && strchr(whitespace, *s))
    s++;
  if(q)
    *q = s;
  ret = *s;
  switch(*s){
  case 0:
    break;
  case '|':
  case '(':
  case ')':
  case ';':
  case '&':
  case '<':
    s++;
    break;
  case '>':
    s++;
    if(*s == '>'){
      ret = '+';
      s++;
    }
    break;
  default:
    ret = 'a';
    while(s < es && !strchr(whitespace, *s) && !strchr(symbols, *s))
      s++;
    break;
  }
  if(eq)
    *eq = s;

  while(s < es && strchr(whitespace, *s))
    s++;
  *ps = s;
  return ret;
}

int
peek(char **ps, char *es, char *toks)
{
  char *s;

  s = *ps;
  while(s < es && strchr(whitespace, *s))
    s++;
  *ps = s;
  return *s && strchr(toks, *s);
}

struct cmd *parseline(char**, char*);
struct cmd *parsepipe(char**, char*);
struct cmd *parseexec(char**, char*);
struct cmd *nulterminate(struct cmd*);

struct cmd*
parsecmd(char *s)
{
  char *es;
  struct cmd *cmd;

  es = s + strlen(s);
  cmd = parseline(&s, es);
  peek(&s, es, "");
  if(s != es){
    printf(2, "leftovers: %s\n", s);
    panic("syntax");
  }
  nulterminate(cmd);
  return cmd;
}

struct cmd*
parseline(char **ps, char *es)
{
  struct cmd *cmd;

  cmd = parsepipe(ps, es);
  while(peek(ps, es, "&")){
    gettoken(ps, es, 0, 0);
    cmd = backcmd(cmd);
  }
  if(peek(ps, es, ";")){
    gettoken(ps, es, 0, 0);
    cmd = listcmd(cmd, parseline(ps, es));
  }
  return cmd;
}

struct cmd*
parsepipe(char **ps, char *es)
{
  struct cmd *cmd;

  cmd = parseexec(ps, es);
  if(peek(ps, es, "|")){
    gettoken(ps, es, 0, 0);
    cmd = pipecmd(cmd, parsepipe(ps, es));
  }
  return cmd;
}

struct cmd*
parseredirs(struct cmd *cmd, char **ps, char *es)
{
  int tok;
  char *q, *eq;

  while(peek(ps, es, "<>")){
    tok = gettoken(ps, es, 0, 0);
    if(gettoken(ps, es, &q, &eq) != 'a')
      panic("missing file for redirection");
    switch(tok){
    case '<':
      cmd = redircmd(cmd, q, eq, O_RDONLY, 0);
      break;
    case '>':
      cmd = redircmd(cmd, q, eq, O_WRONLY|O_CREATE, 1);
      break;
    case '+':  // >>
      cmd = redircmd(cmd, q, eq, O_WRONLY|O_CREATE, 1);
      break;
    }
  }
  return cmd;
}

struct cmd*
parseblock(char **ps, char *es)
{
  struct cmd *cmd;

  if(!peek(ps, es, "("))
    panic("parseblock");
  gettoken(ps, es, 0, 0);
  cmd = parseline(ps, es);
  if(!peek(ps, es, ")"))
    panic("syntax - missing )");
  gettoken(ps, es, 0, 0);
  cmd = parseredirs(cmd, ps, es);
  return cmd;
}

struct cmd*
parseexec(char **ps, char *es)
{
  char *q, *eq;
  int tok, argc;
  struct execcmd *cmd;
  struct cmd *ret;

  if(peek(ps, es, "("))
    return parseblock(ps, es);

  ret = execcmd();
  cmd = (struct execcmd*)ret;

  argc = 0;
  ret = parseredirs(ret, ps, es);
  while(!peek(ps, es, "|)&;")){
    if((tok=gettoken(ps, es, &q, &eq)) == 0)
      break;
    if(tok != 'a')
      panic("syntax");
    cmd->argv[argc] = q;
    cmd->eargv[argc] = eq;
    argc++;
    if(argc >= MAXARGS)
      panic("too many args");
    ret = parseredirs(ret, ps, es);
  }
  cmd->argv[argc] = 0;
  cmd->eargv[argc] = 0;
  return ret;
}

// NUL-terminate all the counted strings.
struct cmd*
nulterminate(struct cmd *cmd)
{
  int i;
  struct backcmd *bcmd;
  struct execcmd *ecmd;
  struct listcmd *lcmd;
  struct pipecmd *pcmd;
  struct redircmd *rcmd;

  if(cmd == 0)
    return 0;

  switch(cmd->type){
  case EXEC:
    ecmd = (struct execcmd*)cmd;
    for(i=0; ecmd->argv[i]; i++)
      *ecmd->eargv[i] = 0;
    break;

  case REDIR:
    rcmd = (struct redircmd*)cmd;
    nulterminate(rcmd->cmd);
    *rcmd->efile = 0;
    break;

  case PIPE:
    pcmd = (struct pipecmd*)cmd;
    nulterminate(pcmd->left);
    nulterminate(pcmd->right);
    break;

  case LIST:
    lcmd = (struct listcmd*)cmd;
    nulterminate(lcmd->left);
    nulterminate(lcmd->right);
    break;

  case BACK:
    bcmd = (struct backcmd*)cmd;
    nulterminate(bcmd->cmd);
    break;
  }
  return cmd;
}
