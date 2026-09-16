#!/usr/bin/env python3
"""Apply the OCI-interop fix to an OpenDDS source tree (both parts).

Part 1: dds/DCPS/XTypes/TypeObject.h  -- Sequence<T> operator>> honors
        skip_sequence_dheader() (symmetric with operator<<).
Part 2: dds/DCPS/RTPS/Sedp.cpp        -- enable skip_sequence_dheader on the
        TypeLookup encodings (send: type_lookup_encoding; receive: the
        encapsulation-derived encoding for TL_SVC entity ids).

Validated offline in research/dds/replay/ (getTypeDependencies_In FAILED->OK).

Usage: python3 apply_patch.py /opt/OpenDDS
"""
import sys, os

def patch_typeobject(root):
    p = os.path.join(root, "dds/DCPS/XTypes/TypeObject.h")
    s = open(p).read()
    old = '''bool operator>>(Serializer& strm, XTypes::Sequence<T>& seq)
{
  size_t total_size = 0;
  if (!strm.read_delimiter(total_size)) {
    return false;
  }'''
    new = '''bool operator>>(Serializer& strm, XTypes::Sequence<T>& seq)
{
  size_t total_size = 0;
  const bool skip_dh = strm.encoding().skip_sequence_dheader();
  if (!skip_dh && !strm.read_delimiter(total_size)) {
    return false;
  }'''
    if old not in s:
        if 'const bool skip_dh = strm.encoding().skip_sequence_dheader();' in s:
            print("  TypeObject.h already patched"); return
        raise SystemExit("TypeObject.h anchor not found")
    s = s.replace(old, new)
    s = s.replace(
'''  if (total_size == 0) {
    seq.length(0);
    return true;
  }

  if (total_size < 4) {
    return false;
  }

  const size_t end_of_seq = strm.rpos() + total_size;''',
'''  if (!skip_dh && total_size == 0) {
    seq.length(0);
    return true;
  }

  if (!skip_dh && total_size < 4) {
    return false;
  }

  const size_t end_of_seq = skip_dh ? 0 : (strm.rpos() + total_size);''')
    s = s.replace(
'''  return strm.skip(end_of_seq - strm.rpos());
}''',
'''  return skip_dh ? true : strm.skip(end_of_seq - strm.rpos());
}''', 1)
    open(p, "w").write(s)
    print("  patched TypeObject.h (Part 1)")

def patch_sedp(root):
    p = os.path.join(root, "dds/DCPS/RTPS/Sedp.cpp")
    s = open(p).read()
    # Part 2a: send-path encoding gets the flag
    old_enc = '  const Encoding type_lookup_encoding(Encoding::KIND_XCDR2, DCPS::ENDIAN_NATIVE);'
    new_enc = ('  Encoding make_type_lookup_encoding_() {\n'
               '    Encoding e(Encoding::KIND_XCDR2, DCPS::ENDIAN_NATIVE);\n'
               '    e.skip_sequence_dheader(true); // OCI interop: peer omits sequence DHEADERs\n'
               '    return e;\n'
               '  }\n'
               '  const Encoding type_lookup_encoding = make_type_lookup_encoding_();')
    if old_enc in s:
        s = s.replace(old_enc, new_enc); print("  patched Sedp.cpp type_lookup_encoding (Part 2a)")
    elif 'make_type_lookup_encoding_' in s:
        print("  Sedp.cpp send-path already patched")
    else:
        raise SystemExit("Sedp.cpp type_lookup_encoding anchor not found")

    # Part 2b: receive-path -- set the flag for TL_SVC entity ids before ser.encoding()
    old_rx = '''    if (!encap.to_encoding(encoding, extensibility)) {
      return;
    }
    ser.encoding(encoding);

    data_received_i(sample, entity_id, ser, extensibility);'''
    new_rx = '''    if (!encap.to_encoding(encoding, extensibility)) {
      return;
    }
    if (entity_id == ENTITYID_TL_SVC_REQ_WRITER ||
        entity_id == ENTITYID_TL_SVC_REPLY_WRITER
#ifdef OPENDDS_SECURITY
        || entity_id == ENTITYID_TL_SVC_REQ_WRITER_SECURE
        || entity_id == ENTITYID_TL_SVC_REPLY_WRITER_SECURE
#endif
        ) {
      encoding.skip_sequence_dheader(true); // OCI interop
    }
    ser.encoding(encoding);

    data_received_i(sample, entity_id, ser, extensibility);'''
    if old_rx in s:
        s = s.replace(old_rx, new_rx); print("  patched Sedp.cpp receive path (Part 2b)")
    elif 'OCI interop' in s and 'skip_sequence_dheader(true); // OCI interop' in s:
        print("  Sedp.cpp receive path already patched")
    else:
        raise SystemExit("Sedp.cpp receive-path anchor not found")
    open(p, "w").write(s)

def main(root):
    print("Applying OCI-interop patch to", root)
    patch_typeobject(root)
    patch_sedp(root)
    print("done")

if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "/opt/OpenDDS")
