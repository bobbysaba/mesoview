#!/bin/bash

# Bobby Saba - reverse tunnel script for data transfer to NSSL web space
# NOTE: this script assumes that the trucks have already obtained the clamps_rsa key

# grab the user defined port
PORT=$1

# validate that a port argument was provided
if [ -z "${PORT}" ]; then
	echo "Usage: $0 <port>" >&2
	exit 1
fi

# set up logging
LOGFILE=$HOME/logs/rtun.$(date +%Y%m%d).log

# define the log function to write timestamped messages to the log file
log() {
	echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "${LOGFILE}"
}

# define a helper to retry a command up to N times with a given sleep interval
# usage: retry_check <max_retries> <sleep_interval> <command>
retry_check() {
	local max=$1
	local interval=$2
	local cmd=$3
	local k=0

	eval ${cmd} >/dev/null 2>&1
	while [[ $? -ne 0 && k -lt ${max} ]]; do
		log "Check failed. k = ${k}. Retrying ..."
		sleep ${interval}
		k=$((k+1))
		eval ${cmd} >/dev/null 2>&1
	done
}

# define the rsa key and user to remote into
USERHOST="-i $HOME/.ssh/clamps_rsa clamps@remote.bliss.science"

# define the ssh command
COMMAND="ssh -q -N -L ${PORT}:localhost:22 -R ${PORT}:localhost:22 -o TCPKeepAlive=yes -o ServerAliveCountMax=3 -o ServerAliveInterval=10 ${USERHOST}"

# log the start
log "=== RTUN.SH started ==="

# define command to check if the tunnel process is already running
check="pgrep -f \"${COMMAND}\""

# check if the tunnel process is running, retrying up to 5 times with 0.2s delay
retry_check 5 0.2 "${check}"

# if the tunnel process is still not running after retries, spawn it
if [ $? -ne 0 ]; then
	log "No tunnel found. Respawning ..."

	# start the tunnel in the background
	eval ${COMMAND} &

	# wait briefly to allow the tunnel process to initialize before verifying
	sleep 2
# if the tunnel process is already running
else
	log "Tunnel exists."
fi

# define command to verify the tunnel is actively forwarding by SSHing through it and checking netstat
check="ssh -o ConnectTimeout=10 -p ${PORT} -i $HOME/.ssh/clamps_rsa clamps@localhost netstat -nlt | grep -E \"127.0.0.1:${PORT}\""

# verify the tunnel is actively forwarding traffic, retrying up to 5 times with 1s delay
retry_check 5 1 "${check}"

# if the tunnel is stalled (process exists but not forwarding)
if [ $? -ne 0 ] ; then
	log "Stalled tunnel. Restarting stalled tunnel ..."

	# find the PID of the stalled tunnel process
	pid=$(pgrep -f "${COMMAND}")

	# kill the stalled tunnel if a PID was found
	if [ -n "${pid}" ]; then
		kill -9 ${pid} >/dev/null 2>&1
	else
		log "Warning: could not find PID for stalled tunnel."
	fi

	# restart the tunnel in the background
	eval ${COMMAND} &

# if the tunnel is verified and healthy
else
	log "Tunnel okay."
fi
