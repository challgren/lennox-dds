// Lennox M30 "LCC Zone Status" OpenDDS-Security subscriber (Phase 4, Backend A).
//
// Creates a secure DomainParticipant on domain 0, joins the homeId partition,
// subscribes to the "LCC Zone Status" topic with the recovered QoS (RELIABLE +
// TRANSIENT_LOCAL + ignore_member_names), waits for one sample, prints it as
// JSON on stdout, and exits. Driven by research/dds/dds_bringup.py, which runs
// it with CWD = research/dds/run/ so the "file:./security/..." property paths
// resolve to the staged Phase-2 bundle.
//
// Build: research/dds/sub/build.sh (needs OpenDDS built with --security; source
// $DDS_ROOT/setenv.sh first). The six DDS-Security documents and their property
// keys are from FINDINGS.md; QoS flags from research/idl/TOPICS.md.
//
// Args:
//   -DCPSConfigFile <ini>   OpenDDS discovery/transport (opendds_rtps.ini)
//   --domain <n>            DDS domain id (default 0)
//   --topic <name>          topic name (default "LCC Zone Status")
//   --partition <homeId>    PartitionQosPolicy value (REQUIRED for real data)
//   --timeout <sec>         seconds to wait for the first sample (default 60)
//   --security-dir <dir>    dir holding the 6 docs (default ./security)

#include <dds/DCPS/Service_Participant.h>
#include <dds/DCPS/Marked_Default_Qos.h>
#include <dds/DCPS/WaitSet.h>
#include <dds/DdsDcpsCoreC.h>
#include <dds/DdsSecurityCoreC.h>

#include "lennox_m30TypeSupportImpl.h"

#include <iostream>
#include <sstream>
#include <string>
#include <cstring>
#include <cstdlib>
#include <csignal>
#include <thread>
#include <vector>

using namespace DDS;
namespace ZS = LxZoneStatusIDL;
namespace SU = LxScheduleUpdateIDL;
namespace SC = LxSchedulesIDL;
namespace PD = Lx_PeriodIDL;

// Period validFlag bits (research/idl/lennox_m30.idl :: Lx_PeriodIDL).
static const unsigned PERIOD_VALID_SYSTEMMODE = 8;
static const unsigned PERIOD_VALID_HSP        = 16;
static const unsigned PERIOD_VALID_CSP        = 64;
static const unsigned PERIOD_VALID_SP         = 256;
static const unsigned PERIOD_VALID_FANMODE    = 16384;
static const unsigned SCHEDULE_VALID_PERIODS = 2;      // Schedule_Valid_Periods
static const unsigned SCHEDULEUPDATE_VALID_SCHEDULE = 1; // ScheduleUpdate_Valid_Schedule

// Persistent writer on the "Owner Schedule Update" topic. Created once (per run)
// and reused for every control command: one-shot --set-csp uses it, and in
// --stream mode a stdin-reader thread drives it from the bridge server's
// commands. Mirrors the app/lennoxs30api payload:
// schedules[0].schedule.periods[0].period.<field>.
struct ScheduleWriter {
  Topic_var topic;
  Publisher_var pub;
  DataWriter_var writer;
  SU::scheduleUpdateDataWriter_var sw;

  bool init(DomainParticipant_var& dp, const std::string& partition) {
    using namespace DDS;
    SU::scheduleUpdateTypeSupport_var ts = new SU::scheduleUpdateTypeSupportImpl();
    if (ts->register_type(dp, "") != RETCODE_OK) { std::cerr << "reg scheduleUpdate failed\n"; return false; }
    CORBA::String_var tn = ts->get_type_name();
    topic = dp->create_topic("Owner Schedule Update", tn, TOPIC_QOS_DEFAULT, 0, 0);
    if (!topic) { std::cerr << "create_topic(Owner Schedule Update) failed\n"; return false; }

    PublisherQos pub_qos; dp->get_default_publisher_qos(pub_qos);
    if (!partition.empty()) { pub_qos.partition.name.length(1); pub_qos.partition.name[0] = partition.c_str(); }
    pub = dp->create_publisher(pub_qos, 0, 0);
    if (!pub) { std::cerr << "create_publisher failed\n"; return false; }

    DataWriterQos dw_qos; pub->get_default_datawriter_qos(dw_qos);
    dw_qos.reliability.kind = RELIABLE_RELIABILITY_QOS;
    dw_qos.durability.kind = VOLATILE_DURABILITY_QOS;
    dw_qos.representation.value.length(1);
    dw_qos.representation.value[0] = XCDR2_DATA_REPRESENTATION;
    writer = pub->create_datawriter(topic, dw_qos, 0, 0);
    if (!writer) { std::cerr << "create_datawriter failed\n"; return false; }
    sw = SU::scheduleUpdateDataWriter::_narrow(writer);
    return !!sw;
  }

  // Wait up to `secs` for the device's schedule reader to associate with us.
  bool wait_for_match(int secs) {
    StatusCondition_var scnd = writer->get_statuscondition();
    scnd->set_enabled_statuses(PUBLICATION_MATCHED_STATUS);
    WaitSet_var ws = new WaitSet; ws->attach_condition(scnd);
    Duration_t wait = { 1, 0 }; ConditionSeq active;
    PublicationMatchedStatus pm; pm.current_count = 0;
    for (int i = 0; i < secs; ++i) {
      writer->get_publication_matched_status(pm);
      if (pm.current_count > 0) break;
      ws->wait(active, wait);
    }
    ws->detach_condition(scnd);
    std::cerr << "[cmd] scheduleUpdate writer matched " << pm.current_count << " reader(s)\n";
    return pm.current_count > 0;
  }

  // Override one or more period setpoints for a zone's schedule. `valid` is the
  // OR of PERIOD_VALID_* bits selecting which of mode/hsp/csp/sp are meaningful.
  bool write_period(const std::string& sys_id, unsigned schedule_id, unsigned valid,
                    int systemMode, double hsp, double csp, double sp, int fanMode) {
    if (!sw) { std::cerr << "[cmd] writer not initialized\n"; return false; }
    if (!wait_for_match(15)) { std::cerr << "[cmd] no matching reader; not writing\n"; return false; }
    if (::getenv("LENNOX_DRY_RUN")) { std::cerr << "[cmd] DRY_RUN: matched OK, skipping write\n"; return true; }

    SU::scheduleUpdate su;
    su.scheduleId = schedule_id;
    su.sysID = sys_id.c_str();
    su.schedule.name = "";
    su.schedule.periods.length(1);
    PD::period& p = su.schedule.periods[0];
    p.id = 0;
    if (valid & PERIOD_VALID_SYSTEMMODE) p.systemMode = static_cast<PD::systemModeEnum>(systemMode);
    if (valid & PERIOD_VALID_HSP)        p.hsp = hsp;
    if (valid & PERIOD_VALID_CSP)        p.csp = csp;
    if (valid & PERIOD_VALID_SP)         p.sp  = sp;
    if (valid & PERIOD_VALID_FANMODE)    p.fanMode = static_cast<PD::fanmodeEnum>(fanMode);
    p.validFlag = valid;
    su.schedule.periodCount = 1;
    su.schedule.validFlag = SCHEDULE_VALID_PERIODS;
    su.validFlag = SCHEDULEUPDATE_VALID_SCHEDULE;

    const ReturnCode_t rc = sw->write(su, HANDLE_NIL);
    std::cerr << "[cmd] WROTE scheduleUpdate scheduleId=" << schedule_id
              << " valid=" << valid << " mode=" << systemMode
              << " hsp=" << hsp << " csp=" << csp << " sp=" << sp
              << " fan=" << fanMode << " rc=" << rc << "\n";
    Duration_t settle = { 3, 0 };
    writer->wait_for_acknowledgments(settle);   // let the reliable protocol deliver
    return rc == RETCODE_OK;
  }
};

// Parse ONE stdin control line and drive the writer. Grammar (from bridge_server):
//   SET <sysID> <scheduleId> [mode=<int>] [csp=<F>] [hsp=<F>] [sp=<F>] [fan=<int>]
// Unknown lines are ignored. Fields present set their PERIOD_VALID_* bit.
static void handle_command_line(ScheduleWriter& sched, const std::string& line) {
  std::istringstream iss(line);
  std::string verb; iss >> verb;
  if (verb != "SET") { std::cerr << "[cmd] ignoring line: " << line << "\n"; return; }
  std::string sys_id; unsigned schedule_id = 0;
  iss >> sys_id >> schedule_id;
  if (sys_id.empty()) { std::cerr << "[cmd] SET missing sysID\n"; return; }
  unsigned valid = 0; int mode = 0, fan = 0; double hsp = 0, csp = 0, sp = 0;
  std::string tok;
  while (iss >> tok) {
    const std::string::size_type eq = tok.find('=');
    if (eq == std::string::npos) continue;
    const std::string k = tok.substr(0, eq), v = tok.substr(eq + 1);
    if      (k == "mode") { mode = std::atoi(v.c_str()); valid |= PERIOD_VALID_SYSTEMMODE; }
    else if (k == "hsp")  { hsp  = std::atof(v.c_str()); valid |= PERIOD_VALID_HSP; }
    else if (k == "csp")  { csp  = std::atof(v.c_str()); valid |= PERIOD_VALID_CSP; }
    else if (k == "sp")   { sp   = std::atof(v.c_str()); valid |= PERIOD_VALID_SP; }
    else if (k == "fan")  { fan  = std::atoi(v.c_str()); valid |= PERIOD_VALID_FANMODE; }
  }
  if (!valid) { std::cerr << "[cmd] SET with no recognized fields\n"; return; }
  sched.write_period(sys_id, schedule_id, valid, mode, hsp, csp, sp, fan);
}

static volatile sig_atomic_t g_running = 1;
static void on_signal(int) { g_running = 0; }

static std::string arg(int argc, char** argv, const char* key, const std::string& def) {
  for (int i = 1; i < argc - 1; ++i) if (!std::strcmp(argv[i], key)) return argv[i + 1];
  return def;
}

static bool has_flag(int argc, char** argv, const char* key) {
  for (int i = 1; i < argc; ++i) if (!std::strcmp(argv[i], key)) return true;
  return false;
}

// Set one PropertyQosPolicy entry.
static void add_prop(PropertyQosPolicy& p, const char* name, const std::string& value) {
  const CORBA::ULong n = p.value.length();
  p.value.length(n + 1);
  p.value[n].name = name;
  p.value[n].value = value.c_str();
  p.value[n].propagate = false; // security props stay local
}

static std::string json_escape(const char* s) {
  std::string o; if (!s) return o;
  for (const char* p = s; *p; ++p) { if (*p == '"' || *p == '\\') o += '\\'; o += *p; }
  return o;
}

static void print_sample_json(const ZS::zoneStatus& z) {
  std::ostringstream o;
  o << "{";
  o << "\"zoneId\":" << z.zoneId;
  o << ",\"sysID\":\"" << json_escape(z.sysID.in()) << "\"";
  o << ",\"temperature\":" << z.temperature;
  o << ",\"temperatureC\":" << z.temperatureC;
  o << ",\"humidity\":" << z.humidity;
  o << ",\"tempOperation\":" << (int)z.tempOperation;
  o << ",\"humOperation\":" << (int)z.humOperation;
  o << ",\"fan\":" << (z.fan ? "true" : "false");
  // status flags (meaningful iff the matching ZoneStatus_Valid_* bit is set;
  // e.g. allergenDefender=256, ventilation=512, aux=1024, ssr=2048, defrost=4096,
  // heatCoast=8192, coolCoast=16384). Streamed for all devices; the HA integration
  // only surfaces the ones the device marks valid.
  o << ",\"allergenDefender\":" << (z.allergenDefender ? "true" : "false");
  o << ",\"ventilation\":" << (z.ventilation ? "true" : "false");
  o << ",\"aux\":" << (z.aux ? "true" : "false");
  o << ",\"ssr\":" << (z.ssr ? "true" : "false");
  o << ",\"defrost\":" << (z.defrost ? "true" : "false");
  o << ",\"heatCoast\":" << (z.heatCoast ? "true" : "false");
  o << ",\"coolCoast\":" << (z.coolCoast ? "true" : "false");
  o << ",\"tempStatus\":" << (int)z.tempStatus;
  o << ",\"humidityStatus\":" << (int)z.humidityStatus;
  o << ",\"balancePoint\":" << (int)z.balancePoint;
  o << ",\"validFlag\":" << z.validFlag;
  // active period setpoints (present iff ZoneStatus_Valid_Period bit set)
  o << ",\"period\":{";
  o << "\"systemMode\":" << (int)z.period.systemMode;
  o << ",\"fanMode\":" << (int)z.period.fanMode;
  o << ",\"sp\":" << z.period.sp << ",\"spC\":" << z.period.spC;
  o << ",\"hsp\":" << z.period.hsp << ",\"hspC\":" << z.period.hspC;
  o << ",\"csp\":" << z.period.csp << ",\"cspC\":" << z.period.cspC;
  o << ",\"husp\":" << z.period.husp << ",\"desp\":" << z.period.desp;
  o << "}";
  o << "}";
  std::cout << o.str() << std::endl;
}

int main(int argc, char** argv) {
  // Service_Participant consumes -DCPSConfigFile and other -DCPS* args.
  DomainParticipantFactory_var dpf = TheParticipantFactoryWithArgs(argc, argv);

  const int domain = std::atoi(arg(argc, argv, "--domain", "0").c_str());
  const std::string topic_name = arg(argc, argv, "--topic", "LCC Zone Status");
  const std::string partition = arg(argc, argv, "--partition", "");
  const int timeout = std::atoi(arg(argc, argv, "--timeout", "60").c_str());
  const std::string secdir = arg(argc, argv, "--security-dir", "./security");

  // --- DomainParticipantQos with the six DDS-Security documents ---
  DomainParticipantQos dp_qos;
  dpf->get_default_participant_qos(dp_qos);
  PropertyQosPolicy& props = dp_qos.property;
  const std::string f = "file:" + secdir + "/";
  add_prop(props, "dds.sec.auth.identity_ca",          f + "identity_ca.pem");
  add_prop(props, "dds.sec.auth.identity_certificate", f + "identity.pem");
  add_prop(props, "dds.sec.auth.private_key",          f + "identity.key");
  add_prop(props, "dds.sec.access.permissions_ca",     f + "permissions_ca.pem");
  add_prop(props, "dds.sec.access.governance",         f + "governance.xml.p7s");
  add_prop(props, "dds.sec.access.permissions",        f + "permissions.xml.p7s");

  DomainParticipant_var dp = dpf->create_participant(domain, dp_qos, 0, 0);
  if (!dp) { std::cerr << "create_participant failed (domain " << domain
                       << ") -- is OpenDDS built with --security and DCPSSecurity=1?\n"; return 2; }

  // --- register the zoneStatus type ---
  ZS::zoneStatusTypeSupport_var ts = new ZS::zoneStatusTypeSupportImpl();
  if (ts->register_type(dp, "") != RETCODE_OK) { std::cerr << "register_type failed\n"; return 3; }
  CORBA::String_var type_name = ts->get_type_name();

  Topic_var topic = dp->create_topic(topic_name.c_str(), type_name, TOPIC_QOS_DEFAULT, 0, 0);
  if (!topic) { std::cerr << "create_topic failed\n"; return 4; }

  // --- Subscriber with PartitionQosPolicy = homeId ---
  SubscriberQos sub_qos;
  dp->get_default_subscriber_qos(sub_qos);
  if (!partition.empty()) {
    sub_qos.partition.name.length(1);
    sub_qos.partition.name[0] = partition.c_str();
  } else {
    std::cerr << "WARN: no --partition (homeId); expect zero samples.\n";
  }
  Subscriber_var sub = dp->create_subscriber(sub_qos, 0, 0);
  if (!sub) { std::cerr << "create_subscriber failed\n"; return 5; }

  // --- DataReader QoS: RELIABLE + TRANSIENT_LOCAL + ignore_member_names (flags 147) ---
  DataReaderQos dr_qos;
  sub->get_default_datareader_qos(dr_qos);
  dr_qos.reliability.kind = RELIABLE_RELIABILITY_QOS;
  dr_qos.durability.kind  = TRANSIENT_LOCAL_DURABILITY_QOS;
  dr_qos.representation.value.length(1);
  dr_qos.representation.value[0] = XCDR2_DATA_REPRESENTATION; // XTypes; adjust if XCDR1
  // (a) maximal type-consistency leniency so a near-but-not-exact recovered IDL
  // still matches the device's type (coerce, ignore bounds + member names, allow
  // widening, don't force full validation).
  dr_qos.type_consistency.kind = ALLOW_TYPE_COERCION;
  dr_qos.type_consistency.ignore_sequence_bounds = true;
  dr_qos.type_consistency.ignore_string_bounds = true;
  dr_qos.type_consistency.ignore_member_names = true;
  dr_qos.type_consistency.prevent_type_widening = false;
  dr_qos.type_consistency.force_type_validation = false;

  DataReader_var reader = sub->create_datareader(topic, dr_qos, 0, 0);
  if (!reader) { std::cerr << "create_datareader failed\n"; return 6; }
  ZS::zoneStatusDataReader_var zr = ZS::zoneStatusDataReader::_narrow(reader);

  // --- optional control write (Phase 6): set cool setpoint via schedule override ---
  if (has_flag(argc, argv, "--set-csp")) {
    const long csp = std::atol(arg(argc, argv, "--set-csp", "0").c_str());
    const unsigned sid = (unsigned)std::atoi(arg(argc, argv, "--schedule-id", "32").c_str());
    const std::string sys = arg(argc, argv, "--sys-id", "");
    if (sys.empty()) {
      std::cerr << "[cmd] --set-csp requires --sys-id <sysID>\n";
    } else {
      std::cerr << "[cmd] setting csp=" << csp << " scheduleId=" << sid << " sys=" << sys << "\n";
      ScheduleWriter oneshot;
      if (oneshot.init(dp, partition))
        oneshot.write_period(sys, sid, PERIOD_VALID_CSP, 0, 0, (double)csp, 0, 0);
    }
  }

  const bool stream = has_flag(argc, argv, "--stream");
  ReadCondition_var rc = reader->create_readcondition(ANY_SAMPLE_STATE, ANY_VIEW_STATE, ALIVE_INSTANCE_STATE);
  WaitSet_var ws = new WaitSet;
  ws->attach_condition(rc);

  // take all currently-available samples, print each as one NDJSON line on
  // stdout (stderr carries logs). Returns how many valid samples were printed.
  auto take_and_print = [&]() -> int {
    ZS::zoneStatusSeq data;
    SampleInfoSeq info;
    if (zr->take_w_condition(data, info, LENGTH_UNLIMITED, rc) != RETCODE_OK) return 0;
    int n = 0;
    for (CORBA::ULong i = 0; i < data.length(); ++i)
      if (info[i].valid_data) { print_sample_json(data[i]); ++n; }
    return n;
  };

  int rc_exit;
  if (stream) {
    // Long-running bridge mode: stream every sample forever until signalled.
    std::signal(SIGINT, on_signal);
    std::signal(SIGTERM, on_signal);
    std::cerr << "[bridge] streaming '" << topic_name << "' (partition='" << partition
              << "', domain " << domain << ")\n";

    // Persistent control writer + stdin command reader. The bridge server writes
    // one "SET ..." line per HA control request to our stdin; each drives a
    // scheduleUpdate. Kept in a detached thread so the sample loop is unblocked.
    ScheduleWriter sched;
    const bool have_writer = sched.init(dp, partition);
    if (!have_writer) std::cerr << "[bridge] control writer init failed; reads-only\n";
    std::thread cmd_thread;
    if (have_writer) {
      cmd_thread = std::thread([&sched]() {
        std::string line;
        while (std::getline(std::cin, line)) {
          if (!line.empty()) handle_command_line(sched, line);
        }
        std::cerr << "[bridge] stdin closed; command channel ended\n";
      });
      cmd_thread.detach();
    }

    Duration_t poll = { 1, 0 };
    while (g_running) {
      ConditionSeq active;
      ws->wait(active, poll);      // RETCODE_TIMEOUT when idle -- fine, just re-loop
      take_and_print();
    }
    std::cerr << "[bridge] shutting down\n";
    rc_exit = 0;
  } else {
    // One-shot test mode: wait up to --timeout for the first sample.
    Duration_t dur = { timeout, 0 };
    ConditionSeq active;
    std::cerr << "waiting up to " << timeout << "s for a '" << topic_name
              << "' sample (partition='" << partition << "', domain " << domain << ")...\n";
    if (ws->wait(active, dur) == RETCODE_TIMEOUT) {
      std::cerr << "TIMEOUT: no sample. Check discovery/partition/security (see BRINGUP.md).\n";
      rc_exit = 7;
    } else {
      rc_exit = take_and_print() > 0 ? 0 : 9;
    }
  }

  ws->detach_condition(rc);
  reader->delete_readcondition(rc);
  dp->delete_contained_entities();
  dpf->delete_participant(dp);
  TheServiceParticipant->shutdown();
  return rc_exit;
}
