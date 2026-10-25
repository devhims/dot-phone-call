// A codec label alone cannot prove that a call preserved high-frequency audio.
const audible = levels => levels && Object.values(levels).length === 2
  && Object.values(levels).every(db => Number.isFinite(db) && db > -65);
const wideband = levels => audible(levels)
  && Math.max(...Object.values(levels)) - Math.min(...Object.values(levels)) < 6;

export function assessProbe(report) {
  const stats = [...(report.caller_stats || []), ...(report.receiver_stats || [])];
  const checks = {
    opus_negotiated: stats.length === 4 && stats.every(s => s.codec?.toLowerCase() === 'audio/opus'
      && s.clockRate === 48000 && s.packets > 0),
    local_reference: wideband(report.reference?.source) && wideband(report.reference?.local_opus),
    two_way_transport: [report.caller_to_receiver, report.receiver_to_caller]
      .every(levels => levels && Object.values(levels).some(db => Number.isFinite(db) && db > -65)),
    cloud_wideband: wideband(report.caller_to_receiver) && wideband(report.receiver_to_caller),
    virtual_wideband: wideband(report.blackhole2_return) && wideband(report.blackhole16_return),
  };
  return {checks, verified: !report.phone_dialed && Object.values(checks).every(Boolean)};
}
