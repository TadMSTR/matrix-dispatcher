module.exports = {
  apps: [
    {
      name: "matrix-dispatcher",
      script: "/home/ted/repos/personal/matrix-dispatcher/start.sh",
      interpreter: "bash",
      cwd: "/home/ted/repos/personal/matrix-dispatcher",
      restart_delay: 5000,
      max_restarts: 10,
      out_file: "/home/ted/.pm2/logs/matrix-dispatcher-out.log",
      error_file: "/home/ted/.pm2/logs/matrix-dispatcher-error.log",
      log_date_format: "YYYY-MM-DD HH:mm:ss",
    },
  ],
};
