# Bash completion for kamailio-node-manage / node-manage.sh
# Installed to /etc/bash_completion.d/ during node-install.sh

_kamailio_node_manage_completions() {
  local cur prev
  cur="${COMP_WORDS[COMP_CWORD]}"
  prev="${COMP_WORDS[COMP_CWORD-1]}"

  local commands="info status start stop restart routing trunks registrations sync-status"
  local components="kamailio rtpengine redis-server fail2ban snmpd all"
  local db="/etc/kamailio/dbsqlite/kamailio.db"

  if [ "$COMP_CWORD" -eq 1 ]; then
    COMPREPLY=( $(compgen -W "$commands" -- "$cur") )
    return 0
  fi

  case "$prev" in
    status|start|stop|restart)
      COMPREPLY=( $(compgen -W "$components" -- "$cur") )
      ;;
    routing)
      if [ -f "$db" ]; then
        local profiles
        profiles=$(sqlite3 "$db" "SELECT name FROM routing_profiles WHERE name IS NOT NULL;" 2>/dev/null)
        COMPREPLY=( $(compgen -W "$profiles" -- "$cur") )
      fi
      ;;
    trunks)
      if [ -f "$db" ]; then
        local trunks
        trunks=$(sqlite3 "$db" "SELECT DISTINCT description FROM dispatcher WHERE description IS NOT NULL;" 2>/dev/null)
        COMPREPLY=( $(compgen -W "$trunks" -- "$cur") )
      fi
      ;;
  esac
}
complete -F _kamailio_node_manage_completions kamailio-node-manage
complete -F _kamailio_node_manage_completions node-manage.sh
