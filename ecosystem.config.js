// PM2 ecosystem file. Paths resolve relative to this file / $HOME so the repo
// is portable — no hardcoded home directory.
const path = require("path");

module.exports = {
  apps: [
    {
      name: "matrix-dispatcher",
      script: path.join(__dirname, "start.sh"),
      interpreter: "bash",
      cwd: __dirname,
      restart_delay: 5000,
      max_restarts: 10,
      out_file: path.join(process.env.HOME, ".pm2/logs/matrix-dispatcher-out.log"),
      error_file: path.join(process.env.HOME, ".pm2/logs/matrix-dispatcher-error.log"),
      log_date_format: "YYYY-MM-DD HH:mm:ss",
    },
  ],
};
